import tensorflow as tf

import numpy as np
from functools import partial

from .base_model_objdet import BaseModel
from ..modules.losses.smooth_L1 import SmoothL1Loss
from ..modules.losses.focal_loss import FocalLoss
from ..modules.losses.cross_entropy import CrossEntropyLoss
from . import Pointnet2MSG, PointnetSAModule, PointnetFPModule
from ..utils.objdet_helper import xywhr_to_xyxyr, box3d_to_bev
from open3d.ml.tf.ops import nms
from ..utils.tf_utils import gen_CNN
from ...datasets.utils import DataProcessing, BEVBox3D
from ...datasets.utils.operations import points_in_box, rotation_3d_in_axis

from ...utils import MODEL
from ..modules.optimizers import OptimWrapper
from ..modules.schedulers import BNMomentumScheduler, OneCycleScheduler, CosineWarmupLR

from ..utils.roipool3d import roipool3d_utils
from ...metrics import iou_3d

class PointRCNN(BaseModel):

    def __init__(self,
                 name="PointRCNN",
                 classes=['Car'],
                 score_thres=0.3,
                 rpn={},
                 rcnn={},
                 mode=None,
                 **kwargs):
        super().__init__(name=name, **kwargs)
        assert mode == "RPN" or mode == "RCNN" or mode == None
        self.mode = mode

        self.classes = classes
        self.name2lbl = {n: i for i, n in enumerate(classes)}
        self.lbl2name = {i: n for i, n in enumerate(classes)}
        self.score_thres = score_thres

        self.rpn = RPN(**rpn)
        self.rcnn = RCNN(num_classes=len(self.classes), **rcnn)

    def call(self, inputs, training=True):
        #with torch.set_grad_enabled(self.training and self.mode == "RPN"):
        cls_score, reg_score, backbone_xyz, backbone_features = self.rpn(
            inputs['point'], training=self.mode=="RPN" and training)

        #with torch.no_grad():
        rpn_scores_raw = cls_score[:, :, 0]
        rois, roi_scores_raw = self.rpn.proposal_layer(
            rpn_scores_raw, reg_score, backbone_xyz)  # (B, M, 7)

        output = {"rois": rois, "cls": cls_score, "reg": reg_score}

        if self.mode == "RCNN":
            #with torch.no_grad():
            rpn_scores_norm = tf.sigmoid(rpn_scores_raw)
            seg_mask = (rpn_scores_norm > self.score_thres).float()
            pts_depth = tf.norm(backbone_xyz, p=2, axis=2)

            output = self.rcnn(rois, inputs.get('bboxes', None), backbone_xyz,
                               tf.tranpose(backbone_features, (0, 2, 1)), seg_mask,
                               pts_depth, training=training)

        return output

    def get_optimizer(self, cfg):

        def children(m):
            return list(m.children())

        def num_children(m) -> int:
            return len(children(m))

        flatten_model = lambda m: sum(map(flatten_model, m.children()), []
                                     ) if num_children(m) else [m]
        get_layer_groups = lambda m: [tf.keras.Sequential(*flatten_model(m))]

        optimizer_func = partial(tf.optimizers.Adam, betas=tuple(cfg.betas))
        optimizer = OptimWrapper.create(optimizer_func,
                                        3e-3,
                                        get_layer_groups(self),
                                        wd=cfg.weight_decay,
                                        true_wd=True,
                                        bn_wd=True)

        # fix rpn: do this since we use customized optimizer.step
        if self.mode == "RCNN":
            for param in self.rpn.parameters():
                param.requires_grad = False

        def bnm_lmbd(cur_epoch):
            cur_decay = 1
            for decay_step in cfg.bn_decay_step_list:
                if cur_epoch >= decay_step:
                    cur_decay = cur_decay * cfg.bn_decay
            return max(cfg.bn_momentum * cur_decay, cfg.bnm_clip)

        lr_scheduler = OneCycleScheduler(optimizer, 40800, cfg.lr,
                                         list(cfg.moms), cfg.div_factor,
                                         cfg.pct_start)

        # bnm_scheduler = BNMomentumScheduler(self.model, bnm_lmbd, last_epoch=last_epoch)

        # lr_warmup_scheduler = CosineWarmupLR(optimizer, T_max=cfg.warmup_epoch * len(train_loader),
        #                                               eta_min=cfg.warmup_min)

        return optimizer, lr_scheduler  #, bnm_scheduler

    def loss(self, results, inputs):
        if self.mode == "RPN":
            return self.rpn.loss(results, inputs)
        else:
            if not self.training:
                return {}
            return self.rcnn.loss(results, inputs)

    def preprocess(self, data, attr):
        # remove intensity
        points = np.array(data['point'][..., :3], dtype=np.float32)
        calib = data['calib']

        # transform in cam space
        points = DataProcessing.world2cam(points, calib['world_cam'])

        return {
            'point': points,
            'bbox_objs': data['bounding_boxes'],
            'calib': data['calib']
        }

    @staticmethod
    def generate_rpn_training_labels(points, bboxes):
        cls_label = np.zeros((points.shape[0]), dtype=np.int32)
        reg_label = np.zeros((points.shape[0], 7),
                             dtype=np.float32)  # dx, dy, dz, ry, h, w, l

        pts_idx = points_in_box(points, bboxes)

        # enlarge the bbox3d, ignore nearby points
        extended_boxes = bboxes.copy()
        extended_boxes[3:6] += 0.4
        extended_boxes[:, 1] += 0.2

        pts_idx_ext = points_in_box(points, extended_boxes)

        for k in range(bboxes.shape[0]):
            fg_pt_flag = pts_idx[:, k]
            fg_pts_rect = points[fg_pt_flag]
            cls_label[fg_pt_flag] = 1

            fg_enlarge_flag = pts_idx_ext[:, k]
            ignore_flag = np.logical_xor(fg_pt_flag, fg_enlarge_flag)
            cls_label[ignore_flag] = -1

            # pixel offset of object center
            center3d = bboxes[k][0:3].copy()  # (x, y, z)
            center3d[1] -= bboxes[k][3] / 2
            reg_label[fg_pt_flag, 0:3] = center3d - fg_pts_rect

            # size and angle encoding
            reg_label[fg_pt_flag, 3] = bboxes[k][3]  # h
            reg_label[fg_pt_flag, 4] = bboxes[k][4]  # w
            reg_label[fg_pt_flag, 5] = bboxes[k][5]  # l
            reg_label[fg_pt_flag, 6] = bboxes[k][6]  # ry

        return cls_label, reg_label

    def transform(self, data, attr):
        points = data['point']

        labels = np.stack([
            self.name2lbl.get(bb.label_class, len(self.classes))
            for bb in data['bbox_objs']
        ])

        bboxes = np.stack([bb.to_camera() for bb in data['bbox_objs']])

        if self.mode == "RPN":
            labels, bboxes = PointRCNN.generate_rpn_training_labels(
                points, bboxes)

        points = tf.constant([points], dtype=tf.float32)
        labels = tf.constant([labels], dtype=tf.int64)
        bboxes = tf.constant([bboxes], dtype=tf.float32)

        if self.mode == "RCNN" and attr['split'] not in ['train', 'training']:
            bboxes = None

        return {
            'point': points,
            'labels': labels,
            'bboxes': bboxes,
            'bbox_objs': data['bbox_objs'],
            'calib': data['calib']
        }

    def inference_end(self, results, inputs):
        if self.mode == 'RPN':
            return [[]]

        roi_boxes3d = results['rois']  # (B, M, 7)
        batch_size = roi_boxes3d.shape[0]

        rcnn_cls = results['cls'].view(batch_size, -1, results['cls'].shape[1])
        rcnn_reg = results['reg'].view(batch_size, -1, results['reg'].shape[1])

        pred_boxes3d, rcnn_cls = self.rcnn.proposal_layer(
            rcnn_cls, rcnn_reg, roi_boxes3d)

        world_cam, cam_img = None, None
        if 'calib' in inputs and inputs['calib'] is not None:
            calib = inputs['calib']
            world_cam = calib.get('world_cam', None)
            cam_img = calib.get('cam_img', None)

        inference_result = []
        for bboxes, scores in zip(pred_boxes3d, rcnn_cls):
            # scoring
            if scores.shape[-1] == 1:
                scores = tf.sigmoid(scores)
                labels = (scores < self.score_thres).long()
            else:
                labels = tf.argmax(scores)
                scores = tf.nn.softmax(scores, axis=0)
                scores = scores[labels]

            fltr = tf.flatten(scores > self.score_thres)
            bboxes = bboxes[fltr]
            labels = labels[fltr]
            scores = scores[fltr]

            bboxes = bboxes.numpy()
            scores = scores.numpy()
            labels = labels.numpy()
            inference_result.append([])

            for bbox, score, label in zip(bboxes, scores, labels):
                pos = bbox[:3]
                dim = bbox[[4, 3, 5]]
                # transform into world space
                pos = DataProcessing.cam2world(pos.reshape((1, -1)),
                                               world_cam).flatten()
                pos = pos + [0, 0, dim[1] / 2]
                yaw = bbox[-1]

                name = self.lbl2name.get(label[0], "ignore")
                inference_result[-1].append(
                    BEVBox3D(pos, dim, yaw, name, score, world_cam, cam_img))

        return inference_result
        
    def get_batch_gen(self, dataset, steps_per_epoch=None, batch_size=1):
        return None


MODEL._register_module(PointRCNN, 'tf')


def get_reg_loss(pred_reg,
                 reg_label,
                 loc_scope,
                 loc_bin_size,
                 num_head_bin,
                 anchor_size,
                 get_xz_fine=True,
                 get_y_by_bin=False,
                 loc_y_scope=0.5,
                 loc_y_bin_size=0.25,
                 get_ry_fine=False):
    """
    Bin-based 3D bounding boxes regression loss. See https://arxiv.org/abs/1812.04244 for more details.
    
    :param pred_reg: (N, C)
    :param reg_label: (N, 7) [dx, dy, dz, h, w, l, ry]
    :param loc_scope: constant
    :param loc_bin_size: constant
    :param num_head_bin: constant
    :param anchor_size: (N, 3) or (3)
    :param get_xz_fine:
    :param get_y_by_bin:
    :param loc_y_scope:
    :param loc_y_bin_size:
    :param get_ry_fine:
    :return:
    """
    per_loc_bin_num = int(loc_scope / loc_bin_size) * 2
    loc_y_bin_num = int(loc_y_scope / loc_y_bin_size) * 2

    reg_loss_dict = {}
    loc_loss = 0

    # xz localization loss
    x_offset_label, y_offset_label, z_offset_label = reg_label[:,
                                                               0], reg_label[:,
                                                                             1], reg_label[:,
                                                                                           2]
    x_shift = tf.clip_by_value(x_offset_label + loc_scope, 0, loc_scope * 2 - 1e-3)
    z_shift = tf.clip_by_value(z_offset_label + loc_scope, 0, loc_scope * 2 - 1e-3)
    x_bin_label = (x_shift / loc_bin_size).floor().long()
    z_bin_label = (z_shift / loc_bin_size).floor().long()

    x_bin_l, x_bin_r = 0, per_loc_bin_num
    z_bin_l, z_bin_r = per_loc_bin_num, per_loc_bin_num * 2
    start_offset = z_bin_r

    loss_x_bin = CrossEntropyLoss()(pred_reg[:, x_bin_l:x_bin_r], x_bin_label)
    loss_z_bin = CrossEntropyLoss()(pred_reg[:, z_bin_l:z_bin_r], z_bin_label)
    reg_loss_dict['loss_x_bin'] = loss_x_bin.item()
    reg_loss_dict['loss_z_bin'] = loss_z_bin.item()
    loc_loss += loss_x_bin + loss_z_bin

    if get_xz_fine:
        x_res_l, x_res_r = per_loc_bin_num * 2, per_loc_bin_num * 3
        z_res_l, z_res_r = per_loc_bin_num * 3, per_loc_bin_num * 4
        start_offset = z_res_r

        x_res_label = x_shift - (x_bin_label.float() * loc_bin_size +
                                 loc_bin_size / 2)
        z_res_label = z_shift - (z_bin_label.float() * loc_bin_size +
                                 loc_bin_size / 2)
        x_res_norm_label = x_res_label / loc_bin_size
        z_res_norm_label = z_res_label / loc_bin_size

        x_bin_onehot = tf.zeros((x_bin_label.shape[0], per_loc_bin_num),
                                   dtype=tf.float32)
        x_bin_onehot.scatter_(1, x_bin_label.view(-1, 1).long(), 1)
        z_bin_onehot = tf.zeros((z_bin_label.shape[0], per_loc_bin_num),
                                   dtype=tf.float32)
        z_bin_onehot.scatter_(1, z_bin_label.view(-1, 1).long(), 1)

        loss_x_res = SmoothL1Loss()(
            (pred_reg[:, x_res_l:x_res_r] * x_bin_onehot).sum(axis=1),
            x_res_norm_label)
        loss_z_res = SmoothL1Loss()(
            (pred_reg[:, z_res_l:z_res_r] * z_bin_onehot).sum(axis=1),
            z_res_norm_label)
        reg_loss_dict['loss_x_res'] = loss_x_res.item()
        reg_loss_dict['loss_z_res'] = loss_z_res.item()
        loc_loss += loss_x_res + loss_z_res

    # y localization loss
    if get_y_by_bin:
        y_bin_l, y_bin_r = start_offset, start_offset + loc_y_bin_num
        y_res_l, y_res_r = y_bin_r, y_bin_r + loc_y_bin_num
        start_offset = y_res_r

        y_shift = tf.clip_by_value(y_offset_label + loc_y_scope, 0,
                              loc_y_scope * 2 - 1e-3)
        y_bin_label = (y_shift / loc_y_bin_size).floor().long()
        y_res_label = y_shift - (y_bin_label.float() * loc_y_bin_size +
                                 loc_y_bin_size / 2)
        y_res_norm_label = y_res_label / loc_y_bin_size

        y_bin_onehot = tf.one_hot(y_bin_label, loc_y_bin_num)

        loss_y_bin = CrossEntropyLoss()(pred_reg[:, y_bin_l:y_bin_r], y_bin_label)
        loss_y_res = SmoothL1Loss()(
            (pred_reg[:, y_res_l:y_res_r] * y_bin_onehot).sum(axis=1),
            y_res_norm_label)

        reg_loss_dict['loss_y_bin'] = loss_y_bin.item()
        reg_loss_dict['loss_y_res'] = loss_y_res.item()

        loc_loss += loss_y_bin + loss_y_res
    else:
        y_offset_l, y_offset_r = start_offset, start_offset + 1
        start_offset = y_offset_r

        loss_y_offset = SmoothL1Loss()(
            pred_reg[:, y_offset_l:y_offset_r].sum(axis=1), y_offset_label)
        reg_loss_dict['loss_y_offset'] = loss_y_offset.item()
        loc_loss += loss_y_offset

    # angle loss
    ry_bin_l, ry_bin_r = start_offset, start_offset + num_head_bin
    ry_res_l, ry_res_r = ry_bin_r, ry_bin_r + num_head_bin

    ry_label = reg_label[:, 6]

    if get_ry_fine:
        # divide pi/2 into several bins
        angle_per_class = (np.pi / 2) / num_head_bin

        ry_label = ry_label % (2 * np.pi)  # 0 ~ 2pi
        opposite_flag = (ry_label > np.pi * 0.5) & (ry_label < np.pi * 1.5)
        ry_label[opposite_flag] = (ry_label[opposite_flag] + np.pi) % (
            2 * np.pi)  # (0 ~ pi/2, 3pi/2 ~ 2pi)
        shift_angle = (ry_label + np.pi * 0.5) % (2 * np.pi)  # (0 ~ pi)

        shift_angle = tf.clip_by_value(shift_angle - np.pi * 0.25,
                                  min=1e-3,
                                  max=np.pi * 0.5 - 1e-3)  # (0, pi/2)

        # bin center is (5, 10, 15, ..., 85)
        ry_bin_label = (shift_angle / angle_per_class).floor().long()
        ry_res_label = shift_angle - (ry_bin_label.float() * angle_per_class +
                                      angle_per_class / 2)
        ry_res_norm_label = ry_res_label / (angle_per_class / 2)

    else:
        # divide 2pi into several bins
        angle_per_class = (2 * np.pi) / num_head_bin
        heading_angle = ry_label % (2 * np.pi)  # 0 ~ 2pi

        shift_angle = (heading_angle + angle_per_class / 2) % (2 * np.pi)
        ry_bin_label = (shift_angle / angle_per_class).floor().long()
        ry_res_label = shift_angle - (ry_bin_label.float() * angle_per_class +
                                      angle_per_class / 2)
        ry_res_norm_label = ry_res_label / (angle_per_class / 2)

    ry_bin_onehot = tf.one_hot(ry_bin_label, num_head_bin)
    loss_ry_bin = CrossEntropyLoss()(pred_reg[:, ry_bin_l:ry_bin_r], ry_bin_label)
    loss_ry_res = SmoothL1Loss()(
        (pred_reg[:, ry_res_l:ry_res_r] * ry_bin_onehot).sum(axis=1),
        ry_res_norm_label)

    reg_loss_dict['loss_ry_bin'] = loss_ry_bin.item()
    reg_loss_dict['loss_ry_res'] = loss_ry_res.item()
    angle_loss = loss_ry_bin + loss_ry_res

    # size loss
    size_res_l, size_res_r = ry_res_r, ry_res_r + 3
    assert pred_reg.shape[1] == size_res_r, '%d vs %d' % (pred_reg.shape[1],
                                                          size_res_r)

    size_res_norm_label = (reg_label[:, 3:6] - anchor_size) / anchor_size
    size_res_norm = pred_reg[:, size_res_l:size_res_r]
    size_loss = SmoothL1Loss()(size_res_norm, size_res_norm_label)

    # Total regression loss
    reg_loss_dict['loss_loc'] = loc_loss
    reg_loss_dict['loss_angle'] = angle_loss
    reg_loss_dict['loss_size'] = size_loss

    return loc_loss, angle_loss, size_loss, reg_loss_dict


class RPN(tf.keras.layers.Layer):

    def __init__(self,
                 backbone={},
                 cls_in_ch=128,
                 cls_out_ch=[128],
                 reg_in_ch=128,
                 reg_out_ch=[128],
                 db_ratio=0.5,
                 head={},
                 focal_loss={},
                 loss_weight=[1.0, 1.0],
                 **kwargs):

        super().__init__()

        # backbone
        self.backbone = Pointnet2MSG(**backbone)
        self.proposal_layer = ProposalLayer(**head)

        # classification branch
        in_filters = [cls_in_ch, *cls_out_ch[:-1]]
        layers = []
        for i in range(len(cls_out_ch)):
            layers.extend([
                tf.keras.layers.Conv1D(cls_out_ch[i], 1, use_bias=False),
                tf.keras.layers.BatchNormalization(),
                tf.keras.layers.ReLU(),
                tf.keras.layers.Dropout(db_ratio)
            ])
        layers.append(tf.keras.layers.Conv1D(1, 1, use_bias=True, bias_initializer=tf.keras.initializers.Constant(-np.log((1 - 0.01) / 0.01))))

        self.cls_blocks = tf.keras.Sequential(layers)

        # regression branch
        per_loc_bin_num = int(self.proposal_layer.loc_scope /
                              self.proposal_layer.loc_bin_size) * 2
        if self.proposal_layer.loc_xz_fine:
            reg_channel = per_loc_bin_num * 4 + self.proposal_layer.num_head_bin * 2 + 3
        else:
            reg_channel = per_loc_bin_num * 2 + self.proposal_layer.num_head_bin * 2 + 3
        reg_channel = reg_channel + 1  # reg y

        in_filters = [reg_in_ch, *reg_out_ch[:-1]]
        layers = []
        for i in range(len(reg_out_ch)):
            layers.extend([
                tf.keras.layers.Conv1D(reg_out_ch[i], 1, use_bias=False),
                tf.keras.layers.BatchNormalization(),
                tf.keras.layers.ReLU(),
                tf.keras.layers.Dropout(db_ratio)
            ])
        layers.append(tf.keras.layers.Conv1D(reg_channel, 1, use_bias=True, kernel_initializer=tf.keras.initializers.RandomNormal(stddev=0.001)))

        self.reg_blocks = tf.keras.Sequential(layers)

        self.loss_cls = FocalLoss(**focal_loss)
        self.loss_weight = loss_weight

    def call(self, x, training=True):
        backbone_xyz, backbone_features = self.backbone(
            x, training=training)  # (B, N, 3), (B, C, N)

        rpn_cls = tf.transpose(self.cls_blocks(backbone_features, training=training), (0, 2, 1))  # (B, N, 1)
        rpn_reg = tf.transpose(self.reg_blocks(backbone_features, training=training), (0, 2, 1))  # (B, N, C)

        return rpn_cls, rpn_reg, backbone_xyz, backbone_features

    def loss(self, results, inputs):
        rpn_cls = results['cls']
        rpn_reg = results['reg']

        rpn_cls_label = inputs['labels']
        rpn_reg_label = inputs['bboxes']

        rpn_cls_label_flat = rpn_cls_label.view(-1)
        rpn_cls_flat = rpn_cls.view(-1)
        fg_mask = (rpn_cls_label_flat > 0)

        # focal loss
        rpn_cls_target = (rpn_cls_label_flat > 0).float()
        pos = (rpn_cls_label_flat > 0).float()
        neg = (rpn_cls_label_flat == 0).float()
        cls_weights = pos + neg
        pos_normalizer = pos.sum()
        cls_weights = cls_weights / tf.clip_by_value(pos_normalizer, min=1.0)
        rpn_loss_cls = self.loss_cls(rpn_cls_flat,
                                     rpn_cls_target,
                                     cls_weights,
                                     avg_factor=1.0)

        # RPN regression loss
        point_num = rpn_reg.shape[0] * rpn_reg.shape[1]
        fg_sum = fg_mask.long().sum().item()
        if fg_sum != 0:
            loss_loc, loss_angle, loss_size, reg_loss_dict = \
                get_reg_loss(rpn_reg.view(point_num, -1)[fg_mask],
                                        rpn_reg_label.view(point_num, 7)[fg_mask],
                                        loc_scope=self.proposal_layer.loc_scope,
                                        loc_bin_size=self.proposal_layer.loc_bin_size,
                                        num_head_bin=self.proposal_layer.num_head_bin,
                                        anchor_size=self.proposal_layer.mean_size,
                                        get_xz_fine=self.proposal_layer.loc_xz_fine,
                                        get_y_by_bin=False,
                                        get_ry_fine=False)

            loss_size = 3 * loss_size
            rpn_loss_reg = loss_loc + loss_angle + loss_size
        else:
            loss_loc = loss_angle = loss_size = rpn_loss_reg = rpn_loss_cls * 0

        return {
            "cls": rpn_loss_cls * self.loss_weight[0],
            "reg": rpn_loss_reg * self.loss_weight[1]
        }

class RCNN(tf.keras.layers.Layer):

    def __init__(
            self,
            num_classes,
            in_channels=128,
            SA_config={
                "npoints": [128, 32, -1],
                "radius": [0.2, 0.4, 100],
                "nsample": [64, 64, 64],
                "mlps": [[128, 128, 128], [128, 128, 256], [256, 256, 512]]
            },
            cls_out_ch=[256, 256],
            reg_out_ch=[256, 256],
            db_ratio=0.5,
            use_xyz=True,
            xyz_up_layer=[128, 128],
            head={},
            target_head={},
            loss={}):

        super().__init__()
        self.rcnn_input_channel = 5

        self.pool_extra_width = target_head.get("pool_extra_width", 1.0)
        self.num_points = target_head.get("num_points", 512)

        self.proposal_layer = ProposalLayer(**head)

        self.SA_modules = []
        for i in range(len(SA_config["npoints"])):
            mlps = [in_channels] + SA_config["mlps"][i]
            npoint = SA_config["npoints"][
                i] if SA_config["npoints"][i] != -1 else None
            self.SA_modules.append(
                PointnetSAModule(npoint=npoint,
                                 radius=SA_config["radius"][i],
                                 nsample=SA_config["nsample"][i],
                                 mlp=mlps,
                                 use_xyz=use_xyz,
                                 use_bias=True))
            in_channels = mlps[-1]

        self.xyz_up_layer = gen_CNN([self.rcnn_input_channel] + xyz_up_layer,
                                    conv=tf.keras.layers.Conv2D)
        c_out = xyz_up_layer[-1]
        self.merge_down_layer = gen_CNN([c_out * 2, c_out], conv=tf.keras.layers.Conv2D)

        # classification layer
        cls_channel = 1 if num_classes == 2 else num_classes

        in_filters = [in_channels, *cls_out_ch[:-1]]
        layers = []
        for i in range(len(cls_out_ch)):
            layers.extend([
                tf.keras.layers.Conv1D(cls_out_ch[i], 1, use_bias=True, kernel_initializer=tf.keras.initializers.GlorotNormal(), bias_initializer=tf.keras.initializers.Constant(0.0)),
                tf.keras.layers.ReLU()
            ])
        layers.append(tf.keras.layers.Conv1D(cls_channel, 1, use_bias=True, kernel_initializer=tf.keras.initializers.GlorotNormal(), bias_initializer=tf.keras.initializers.Constant(0.0)))

        self.cls_blocks = tf.keras.Sequential(layers)

        self.loss_cls = tf.keras.losses.BinaryCrossentropy()

        # regression branch
        per_loc_bin_num = int(self.proposal_layer.loc_scope /
                              self.proposal_layer.loc_bin_size) * 2
        loc_y_bin_num = int(self.proposal_layer.loc_y_scope /
                            self.proposal_layer.loc_y_bin_size) * 2
        reg_channel = per_loc_bin_num * 4 + self.proposal_layer.num_head_bin * 2 + 3
        reg_channel += (1 if not self.proposal_layer.get_y_by_bin else
                        loc_y_bin_num * 2)

        in_filters = [in_channels, *reg_out_ch[:-1]]
        layers = []
        for i in range(len(reg_out_ch)):
            layers.extend([
                tf.keras.layers.Conv1D(reg_out_ch[i], 1, use_bias=True, kernel_initializer=tf.keras.initializers.GlorotNormal(), bias_initializer=tf.keras.initializers.Constant(0.0)),
                tf.keras.layers.ReLU()
            ])
        layers.append(tf.keras.layers.Conv1D(reg_channel, 1, use_bias=True, kernel_initializer=tf.keras.initializers.RandomNormal(stddev=0.001), bias_initializer=tf.keras.initializers.Constant(0.0)))

        self.reg_blocks = tf.keras.Sequential(layers)

        self.proposal_target_layer = ProposalTargetLayer(**target_head)

    def _break_up_pc(self, pc):
        xyz = pc[..., 0:3]
        features = (tf.transpose(pc[..., 3:], (0, 2, 1))
                    if pc.shape[-1] > 3 else None)

        return xyz, features

    def call(self, roi_boxes3d, gt_boxes3d, rpn_xyz, rpn_features, seg_mask,
                pts_depth, training=True):
        pts_extra_input_list = [tf.expand_dims(seg_mask, axis=2)]
        pts_extra_input_list.append(tf.expand_dims(pts_depth / 70.0 - 0.5, axis=2))
        pts_extra_input = tf.concat(pts_extra_input_list, axis=2)
        pts_feature = tf.concat((pts_extra_input, rpn_features), axis=2)

        if gt_boxes3d is not None:
            #with torch.no_grad():
            target = self.proposal_target_layer(
                [roi_boxes3d, gt_boxes3d, rpn_xyz, pts_feature])
            pts_input = tf.concat(
                (target['sampled_pts'], target['pts_feature']), axis=2)
            target['pts_input'] = pts_input
        else:
            pooled_features, pooled_empty_flag = roipool3d_utils.roipool3d_gpu(
                rpn_xyz,
                pts_feature,
                roi_boxes3d,
                self.pool_extra_width,
                sampled_pt_num=self.num_points)

            # canonical transformation
            batch_size = roi_boxes3d.shape[0]
            roi_center = roi_boxes3d[:, :, 0:3]
            pooled_features[:, :, :, 0:3] -= tf.expand_dims(roi_center, axis=2)
            for k in range(batch_size):
                pooled_features[k, :, :, 0:3] = rotate_pc_along_y_tf(
                    pooled_features[k, :, :, 0:3], roi_boxes3d[k, :, 6])

            pts_input = pooled_features.view(-1, pooled_features.shape[2],
                                             pooled_features.shape[3])

        xyz, features = self._break_up_pc(pts_input)

        xyz_input = tf.expand_dims(tf.transpose(pts_input[..., 0:self.rcnn_input_channel], (0, 2, 1)), axis=3)
        xyz_feature = self.xyz_up_layer(xyz_input, training=training)

        rpn_feature = tf.expand_dims(tf.transpose(pts_input[..., self.rcnn_input_channel:], (0, 2, 1)), axis=3)

        merged_feature = tf.concat((xyz_feature, rpn_feature), axis=1)
        merged_feature = self.merge_down_layer(merged_feature, training=training)
        l_xyz, l_features = [xyz], [tf.squeeze(merged_feature, axis=3)]

        for i in range(len(self.SA_modules)):
            li_xyz, li_features = self.SA_modules[i](l_xyz[i], l_features[i], training=training)
            l_xyz.append(li_xyz)
            l_features.append(li_features)

        rcnn_cls = tf.squeeze(tf.tranpose(self.cls_blocks(l_features[-1], training=training), (
            1, 2)), axis=1)  # (B, 1 or 2)
        rcnn_reg = tf.squeeze(tf.tranpose(self.reg_blocks(l_features[-1], training=training), (
            1, 2)), axis=1)  # (B, C)

        ret_dict = {'rois': roi_boxes3d, 'cls': rcnn_cls, 'reg': rcnn_reg}

        if gt_boxes3d is not None:
            ret_dict.update(target)
        return ret_dict

    def loss(self, results, inputs):
        rcnn_cls = results['cls']
        rcnn_reg = results['reg']

        cls_label = results['cls_label'].float()
        reg_valid_mask = results['reg_valid_mask']
        roi_boxes3d = results['roi_boxes3d']
        roi_size = roi_boxes3d[:, 3:6]
        gt_boxes3d_ct = results['gt_of_rois']
        pts_input = results['pts_input']

        cls_label_flat = cls_label.view(-1)

        # binary cross entropy
        rcnn_cls_flat = rcnn_cls.view(-1)
        batch_loss_cls = tf.keras.losses.BinaryCrossentropy()(tf.sigmoid(rcnn_cls_flat),
                                                cls_label,
                                                reduction='none')
        cls_valid_mask = (cls_label_flat >= 0).float()
        rcnn_loss_cls = (batch_loss_cls * cls_valid_mask).sum() / tf.clip_by_value(
            cls_valid_mask.sum(), min=1.0)

        # rcnn regression loss
        batch_size = pts_input.shape[0]
        fg_mask = (reg_valid_mask > 0)
        fg_sum = fg_mask.long().sum().item()
        if fg_sum != 0:
            all_anchor_size = roi_size
            anchor_size = self.proposal_layer.mean_size

            loss_loc, loss_angle, loss_size, reg_loss_dict = \
                get_reg_loss(rcnn_reg.view(batch_size, -1)[fg_mask],
                                        gt_boxes3d_ct.view(batch_size, 7)[fg_mask],
                                        loc_scope=self.proposal_layer.loc_scope,
                                        loc_bin_size=self.proposal_layer.loc_bin_size,
                                        num_head_bin=self.proposal_layer.num_head_bin,
                                        anchor_size=anchor_size,
                                        get_xz_fine=True, get_y_by_bin=self.proposal_layer.get_y_by_bin,
                                        loc_y_scope=self.proposal_layer.loc_y_scope, loc_y_bin_size=self.proposal_layer.loc_y_bin_size,
                                        get_ry_fine=True)

            loss_size = 3 * loss_size  # consistent with old codes
            rcnn_loss_reg = loss_loc + loss_angle + loss_size
        else:
            loss_loc = loss_angle = loss_size = rcnn_loss_reg = rcnn_loss_cls * 0

        return {"cls": rcnn_loss_cls, "reg": rcnn_loss_reg}


def rotate_pc_along_y(pc, rot_angle):
    """
    params pc: (N, 3+C), (N, 3) is in the rectified camera coordinate
    params rot_angle: rad scalar
    Output pc: updated pc with XYZ rotated
    """
    cosval = np.cos(rot_angle)
    sinval = np.sin(rot_angle)
    rotmat = np.array([[cosval, -sinval], [sinval, cosval]])
    pc[:, [0, 2]] = np.dot(pc[:, [0, 2]], np.transpose(rotmat))
    return pc


class ProposalLayer(tf.keras.layers.Layer):

    def __init__(self,
                 nms_pre=9000,
                 nms_post=512,
                 nms_thres=0.8,
                 mean_size=[1.0],
                 loc_xz_fine=True,
                 loc_scope=3.0,
                 loc_bin_size=0.5,
                 num_head_bin=12,
                 get_y_by_bin=False,
                 get_ry_fine=False,
                 loc_y_scope=0.5,
                 loc_y_bin_size=0.25,
                 post_process=True):
        super().__init__()
        self.nms_pre = nms_pre
        self.nms_post = nms_post
        self.nms_thres = nms_thres
        self.mean_size = tf.constant(mean_size)
        self.loc_scope = loc_scope
        self.loc_bin_size = loc_bin_size
        self.num_head_bin = num_head_bin
        self.loc_xz_fine = loc_xz_fine
        self.get_y_by_bin = get_y_by_bin
        self.get_ry_fine = get_ry_fine
        self.loc_y_scope = loc_y_scope
        self.loc_y_bin_size = loc_y_bin_size
        self.post_process = post_process

    def call(self, rpn_scores, rpn_reg, xyz):
        batch_size = xyz.shape[0]
        proposals = decode_bbox_target(
            xyz.view(-1, xyz.shape[-1]),
            rpn_reg.view(-1, rpn_reg.shape[-1]),
            anchor_size=self.mean_size,
            loc_scope=self.loc_scope,
            loc_bin_size=self.loc_bin_size,
            num_head_bin=self.num_head_bin,
            get_xz_fine=self.loc_xz_fine,
            get_y_by_bin=self.get_y_by_bin,
            get_ry_fine=self.get_ry_fine,
            loc_y_scope=self.loc_y_scope,
            loc_y_bin_size=self.loc_y_bin_size)  # (N, 7)

        proposals = proposals.view(batch_size, -1, 7)

        if self.post_process:
            proposals[...,
                      1] += proposals[...,
                                      3] / 2  # set y as the center of bottom
            scores = rpn_scores
            _, sorted_idxs = tf.sort(scores, axis=1, direction="DESCENDING")

            batch_size = scores.shape[0]
            ret_bbox3d = tf.zeros((batch_size, self.nms_post, 7), dtype=scores.dtype)
            ret_scores = tf.zeros((batch_size, self.nms_post), dtype=scores.dtype)
            for k in range(batch_size):
                scores_single = scores[k]
                proposals_single = proposals[k]
                order_single = sorted_idxs[k]

                scores_single, proposals_single = self.distance_based_proposal(
                    scores_single, proposals_single, order_single)

                proposals_tot = proposals_single.shape[0]
                ret_bbox3d[k, :proposals_tot] = proposals_single
                ret_scores[k, :proposals_tot] = scores_single
        else:
            batch_size = rpn_scores.shape[0]
            ret_bbox3d = []
            ret_scores = []
            for k in range(batch_size):
                bev = xywhr_to_xyxyr(proposals[k, :, [0, 2, 3, 5, 6]])
                keep_idx = nms(bev, rpn_scores[k], self.nms_thres)

                ret_bbox3d.append(proposals[k, keep_idx])
                ret_scores.append(rpn_scores[k, keep_idx])

        return ret_bbox3d, ret_scores

    def distance_based_proposal(self, scores, proposals, order):
        """
         propose rois in two area based on the distance
        :param scores: (N)
        :param proposals: (N, 7)
        :param order: (N)
        """
        nms_range_list = [0, 40.0, 80.0]
        pre_top_n_list = [
            0,
            int(self.nms_pre * 0.7), self.nms_pre - int(self.nms_pre * 0.7)
        ]
        post_top_n_list = [
            0,
            int(self.nms_post * 0.7), self.nms_post - int(self.nms_post * 0.7)
        ]

        scores_single_list, proposals_single_list = [], []

        # sort by score
        scores_ordered = scores[order]
        proposals_ordered = proposals[order]

        dist = proposals_ordered[:, 2]
        first_mask = (dist > nms_range_list[0]) & (dist <= nms_range_list[1])
        for i in range(1, len(nms_range_list)):
            # get proposal distance mask
            dist_mask = ((dist > nms_range_list[i - 1]) &
                         (dist <= nms_range_list[i]))

            if dist_mask.sum() != 0:
                # this area has points
                # reduce by mask
                cur_scores = scores_ordered[dist_mask]
                cur_proposals = proposals_ordered[dist_mask]

                # fetch pre nms top K
                cur_scores = cur_scores[:pre_top_n_list[i]]
                cur_proposals = cur_proposals[:pre_top_n_list[i]]
            else:
                assert i == 2, '%d' % i
                # this area doesn't have any points, so use rois of first area
                cur_scores = scores_ordered[first_mask]
                cur_proposals = proposals_ordered[first_mask]

                # fetch top K of first area
                cur_scores = cur_scores[pre_top_n_list[i -
                                                       1]:][:pre_top_n_list[i]]
                cur_proposals = cur_proposals[
                    pre_top_n_list[i - 1]:][:pre_top_n_list[i]]

            # oriented nms
            bev = xywhr_to_xyxyr(cur_proposals[:, [0, 2, 3, 5, 6]])
            keep_idx = nms(bev, cur_scores, self.nms_thres)

            # Fetch post nms top k
            keep_idx = keep_idx[:post_top_n_list[i]]

            scores_single_list.append(cur_scores[keep_idx])
            proposals_single_list.append(cur_proposals[keep_idx])

        scores_single = tf.concat(scores_single_list, axis=0)
        proposals_single = tf.cocat(proposals_single_list, axis=0)
        return scores_single, proposals_single


def decode_bbox_target(roi_box3d,
                       pred_reg,
                       loc_scope,
                       loc_bin_size,
                       num_head_bin,
                       anchor_size,
                       get_xz_fine=True,
                       get_y_by_bin=False,
                       loc_y_scope=0.5,
                       loc_y_bin_size=0.25,
                       get_ry_fine=False):
    """
    :param roi_box3d: (N, 7)
    :param pred_reg: (N, C)
    :param loc_scope:
    :param loc_bin_size:
    :param num_head_bin:
    :param anchor_size:
    :param get_xz_fine:
    :param get_y_by_bin:
    :param loc_y_scope:
    :param loc_y_bin_size:
    :param get_ry_fine:
    :return:
    """
    per_loc_bin_num = int(loc_scope / loc_bin_size) * 2
    loc_y_bin_num = int(loc_y_scope / loc_y_bin_size) * 2

    # recover xz localization
    x_bin_l, x_bin_r = 0, per_loc_bin_num
    z_bin_l, z_bin_r = per_loc_bin_num, per_loc_bin_num * 2
    start_offset = z_bin_r

    x_bin = tf.argmax(pred_reg[:, x_bin_l:x_bin_r], axis=1)
    z_bin = tf.argmax(pred_reg[:, z_bin_l:z_bin_r], axis=1)

    pos_x = x_bin.float() * loc_bin_size + loc_bin_size / 2 - loc_scope
    pos_z = z_bin.float() * loc_bin_size + loc_bin_size / 2 - loc_scope

    if get_xz_fine:
        x_res_l, x_res_r = per_loc_bin_num * 2, per_loc_bin_num * 3
        z_res_l, z_res_r = per_loc_bin_num * 3, per_loc_bin_num * 4
        start_offset = z_res_r

        x_res_norm = tf.squeeze(tf.gather_nd(pred_reg[:, x_res_l:x_res_r],
                                  axis=1,
                                  index=tf.expand_dims(x_bin, axis=1)), axis=1)
        z_res_norm = tf.squeeze(tf.gather_nd(pred_reg[:, z_res_l:z_res_r],
                                  axis=1,
                                  index=tf.expand_dims(z_bin, axis=1)), axis=1)
        x_res = x_res_norm * loc_bin_size
        z_res = z_res_norm * loc_bin_size

        pos_x += x_res
        pos_z += z_res

    # recover y localization
    if get_y_by_bin:
        y_bin_l, y_bin_r = start_offset, start_offset + loc_y_bin_num
        y_res_l, y_res_r = y_bin_r, y_bin_r + loc_y_bin_num
        start_offset = y_res_r

        y_bin = tf.argmax(pred_reg[:, y_bin_l:y_bin_r], axis=1)
        y_res_norm = tf.squeeze(tf.gather(pred_reg[:, y_res_l:y_res_r],
                                  axis=1,
                                  index=tf.expand_dims(y_bin, axis=1)), axis=1)
        y_res = y_res_norm * loc_y_bin_size
        pos_y = y_bin.float(
        ) * loc_y_bin_size + loc_y_bin_size / 2 - loc_y_scope + y_res
        pos_y = pos_y + roi_box3d[:, 1]
    else:
        y_offset_l, y_offset_r = start_offset, start_offset + 1
        start_offset = y_offset_r

        pos_y = roi_box3d[:, 1] + pred_reg[:, y_offset_l]

    # recover ry rotation
    ry_bin_l, ry_bin_r = start_offset, start_offset + num_head_bin
    ry_res_l, ry_res_r = ry_bin_r, ry_bin_r + num_head_bin

    ry_bin = tf.argmax(pred_reg[:, ry_bin_l:ry_bin_r], axis=1)
    ry_res_norm = tf.squeeze(tf.gather_nd(pred_reg[:, ry_res_l:ry_res_r],
                               axis=1,
                               index=tf.expand_dims(ry_bin, axis=1)), axis=1)
    if get_ry_fine:
        # divide pi/2 into several bins
        angle_per_class = (np.pi / 2) / num_head_bin
        ry_res = ry_res_norm * (angle_per_class / 2)
        ry = (ry_bin.float() * angle_per_class +
              angle_per_class / 2) + ry_res - np.pi / 4
    else:
        angle_per_class = (2 * np.pi) / num_head_bin
        ry_res = ry_res_norm * (angle_per_class / 2)

        # bin_center is (0, 30, 60, 90, 120, ..., 270, 300, 330)
        ry = (ry_bin.float() * angle_per_class + ry_res) % (2 * np.pi)
        ry[ry > np.pi] -= 2 * np.pi

    # recover size
    size_res_l, size_res_r = ry_res_r, ry_res_r + 3
    assert size_res_r == pred_reg.shape[1]

    size_res_norm = pred_reg[:, size_res_l:size_res_r]
    hwl = size_res_norm * anchor_size + anchor_size

    # shift to original coords
    roi_center = roi_box3d[:, 0:3]
    shift_ret_box3d = tf.concat((pos_x.view(-1, 1), pos_y.view(
        -1, 1), pos_z.view(-1, 1), hwl, ry.view(-1, 1)),
                                axis=1)
    ret_box3d = shift_ret_box3d
    if roi_box3d.shape[1] == 7:
        roi_ry = roi_box3d[:, 6]
        ret_box3d = rotate_pc_along_y_torch(shift_ret_box3d, -roi_ry)
        ret_box3d[:, 6] += roi_ry
    ret_box3d[:, [0, 2]] += roi_center[:, [0, 2]]

    return ret_box3d


def rotate_pc_along_y_torch(pc, rot_angle):
    """
    :param pc: (N, 3 + C)
    :param rot_angle: (N)
    :return:
    """
    cosa = tf.cos(rot_angle).view(-1, 1)  # (N, 1)
    sina = tf.sin(rot_angle).view(-1, 1)  # (N, 1)

    raw_1 = tf.concat([cosa, -sina], axis=1)  # (N, 2)
    raw_2 = tf.concat([sina, cosa], axis=1)  # (N, 2)
    R = tf.concat((tf.expand_dims(raw_1, axis=1), tf.expand_dims(raw_2, axis=1)),
                  axis=1)  # (N, 2, 2)

    pc_temp = pc[..., [0, 2]].view((pc.shape[0], -1, 2))  # (N, 512, 2)

    pc[..., [0, 2]] = tf.matmul(pc_temp, R.permute(0, 2, 1)).view(
        pc.shape[:-1] + (2,))  # (N, 512, 2)

    return pc


class ProposalTargetLayer(tf.keras.layers.Layer):

    def __init__(self,
                 pool_extra_width=1.0,
                 num_points=512,
                 reg_fg_thresh=0.55,
                 cls_fg_thresh=0.6,
                 cls_bg_thresh=0.45,
                 cls_bg_thresh_lo=0.05,
                 fg_ratio=0.5,
                 roi_per_image=64,
                 aug_rot_range=18,
                 hard_bg_ratio=0.8,
                 roi_fg_aug_times=10):
        super().__init__()
        self.pool_extra_width = pool_extra_width
        self.num_points = num_points
        self.reg_fg_thresh = reg_fg_thresh
        self.cls_fg_thresh = cls_fg_thresh
        self.cls_bg_thresh = cls_bg_thresh
        self.cls_bg_thresh_lo = cls_bg_thresh_lo
        self.fg_ratio = fg_ratio
        self.roi_per_image = roi_per_image
        self.aug_rot_range = aug_rot_range
        self.hard_bg_ratio = hard_bg_ratio
        self.roi_fg_aug_times = roi_fg_aug_times

    def call(self, x):
        roi_boxes3d, gt_boxes3d, rpn_xyz, pts_feature = x
        batch_rois, batch_gt_of_rois, batch_roi_iou = self.sample_rois_for_rcnn(
            roi_boxes3d, gt_boxes3d)

        # point cloud pooling
        pooled_features, pooled_empty_flag = \
            roipool3d_utils.roipool3d_gpu(rpn_xyz, pts_feature, batch_rois, self.pool_extra_width,
                                          sampled_pt_num=self.num_points)

        sampled_pts, sampled_features = pooled_features[:, :, :, 0:
                                                        3], pooled_features[:, :, :,
                                                                            3:]

        # data augmentation
        sampled_pts, batch_rois, batch_gt_of_rois = \
            self.data_augmentation(sampled_pts, batch_rois, batch_gt_of_rois)

        # canonical transformation
        batch_size = batch_rois.shape[0]
        roi_ry = batch_rois[:, :, 6] % (2 * np.pi)
        roi_center = batch_rois[:, :, 0:3]
        sampled_pts = sampled_pts - tf.expand_dims(roi_center, axis=2)  # (B, M, 512, 3)
        batch_gt_of_rois[:, :, 0:3] = batch_gt_of_rois[:, :, 0:3] - roi_center
        batch_gt_of_rois[:, :, 6] = batch_gt_of_rois[:, :, 6] - roi_ry

        for k in range(batch_size):
            sampled_pts[k] = rotate_pc_along_y_torch(sampled_pts[k],
                                                     batch_rois[k, :, 6])
            batch_gt_of_rois[k] = tf.squeeze(rotate_pc_along_y_torch(
                tf.expand_dims(batch_gt_of_rois[k], axis=1), roi_ry[k]), axis=1)

        # regression valid mask
        valid_mask = (pooled_empty_flag == 0)
        reg_valid_mask = ((batch_roi_iou > self.reg_fg_thresh) &
                          valid_mask).long()

        # classification label
        batch_cls_label = (batch_roi_iou > self.cls_fg_thresh).long()
        invalid_mask = (batch_roi_iou > self.cls_bg_thresh) & (
            batch_roi_iou < self.cls_fg_thresh)
        batch_cls_label[valid_mask == 0] = -1
        batch_cls_label[invalid_mask > 0] = -1

        output_dict = {
            'sampled_pts':
                sampled_pts.view(-1, self.num_points, 3),
            'pts_feature':
                sampled_features.view(-1, self.num_points,
                                      sampled_features.shape[3]),
            'cls_label':
                batch_cls_label.view(-1),
            'reg_valid_mask':
                reg_valid_mask.view(-1),
            'gt_of_rois':
                batch_gt_of_rois.view(-1, 7),
            'gt_iou':
                batch_roi_iou.view(-1),
            'roi_boxes3d':
                batch_rois.view(-1, 7)
        }

        return output_dict

    def sample_rois_for_rcnn(self, roi_boxes3d, gt_boxes3d):
        """
        :param roi_boxes3d: (B, M, 7)
        :param gt_boxes3d: (B, N, 8) [x, y, z, h, w, l, ry, cls]
        :return
            batch_rois: (B, N, 7)
            batch_gt_of_rois: (B, N, 8)
            batch_roi_iou: (B, N)
        """
        batch_size = roi_boxes3d.shape[0]

        fg_rois_per_image = int(np.round(self.fg_ratio * self.roi_per_image))

        batch_rois = tf.zeros((batch_size, self.roi_per_image, 7), dype=gt_boxes3d.dtype)
        batch_gt_of_rois = tf.zeros((batch_size, self.roi_per_image,
                                          7), dype=gt_boxes3d.dtype)
        batch_roi_iou = tf.zeros((batch_size, self.roi_per_image), dype=gt_boxes3d.dtype)

        for idx in range(batch_size):
            cur_roi, cur_gt = roi_boxes3d[idx], gt_boxes3d[idx]

            k = cur_gt.__len__() - 1
            while cur_gt[k].sum() == 0:
                k -= 1
            cur_gt = cur_gt[:k + 1]

            # include gt boxes in the candidate rois
            iou3d = iou_3d(
                cur_roi.detach().cpu().numpy()[:, [0, 1, 2, 5, 3, 4, 6]],
                cur_gt[:, 0:7].detach().cpu().numpy()
                [:, [0, 1, 2, 5, 3, 4, 6]])  # (M, N)
            iou3d = tf.constant(iou3d)

            max_overlaps, gt_assignment = tf.max(iou3d, axis=1)

            # sample fg, easy_bg, hard_bg
            fg_thresh = min(self.reg_fg_thresh, self.cls_fg_thresh)
            fg_inds = tf.where((max_overlaps >= fg_thresh)).view(-1)

            # TODO: this will mix the fg and bg when CLS_BG_THRESH_LO < iou < CLS_BG_THRESH
            # fg_inds = tf.concat((fg_inds, roi_assignment), axis=0)  # consider the roi which has max_iou with gt as fg

            easy_bg_inds = tf.where(
                (max_overlaps < self.cls_bg_thresh_lo)).view(-1)
            hard_bg_inds = tf.where((max_overlaps < self.cls_bg_thresh) & (
                max_overlaps >= self.cls_bg_thresh_lo)).view(-1)

            fg_num_rois = fg_inds.numel()
            bg_num_rois = hard_bg_inds.numel() + easy_bg_inds.numel()

            if fg_num_rois > 0 and bg_num_rois > 0:
                # sampling fg
                fg_rois_per_this_image = min(fg_rois_per_image, fg_num_rois)

                rand_num = tf.constant(np.random.permutation(
                    fg_num_rois), dtype=tf.int64)
                fg_inds = fg_inds[rand_num[:fg_rois_per_this_image]]

                # sampling bg
                bg_rois_per_this_image = self.roi_per_image - fg_rois_per_this_image
                bg_inds = self.sample_bg_inds(hard_bg_inds, easy_bg_inds,
                                              bg_rois_per_this_image)

            elif fg_num_rois > 0 and bg_num_rois == 0:
                # sampling fg
                rand_num = np.floor(
                    np.random.rand(self.roi_per_image) * fg_num_rois)
                rand_num = tf.constant(rand_num, dtype=tf.int64)
                fg_inds = fg_inds[rand_num]
                fg_rois_per_this_image = self.roi_per_image
                bg_rois_per_this_image = 0
            elif bg_num_rois > 0 and fg_num_rois == 0:
                # sampling bg
                bg_rois_per_this_image = self.roi_per_image
                bg_inds = self.sample_bg_inds(hard_bg_inds, easy_bg_inds,
                                              bg_rois_per_this_image)

                fg_rois_per_this_image = 0
            else:
                import pdb
                pdb.set_trace()
                raise NotImplementedError

            # augment the rois by noise
            roi_list, roi_iou_list, roi_gt_list = [], [], []
            if fg_rois_per_this_image > 0:
                fg_rois_src = cur_roi[fg_inds]
                gt_of_fg_rois = cur_gt[gt_assignment[fg_inds]]
                iou3d_src = max_overlaps[fg_inds]
                fg_rois, fg_iou3d = self.aug_roi_by_noise_torch(
                    fg_rois_src,
                    gt_of_fg_rois,
                    iou3d_src,
                    aug_times=self.roi_fg_aug_times)
                roi_list.append(fg_rois)
                roi_iou_list.append(fg_iou3d)
                roi_gt_list.append(gt_of_fg_rois)

            if bg_rois_per_this_image > 0:
                bg_rois_src = cur_roi[bg_inds]
                gt_of_bg_rois = cur_gt[gt_assignment[bg_inds]]
                iou3d_src = max_overlaps[bg_inds]
                aug_times = 1 if self.roi_fg_aug_times > 0 else 0
                bg_rois, bg_iou3d = self.aug_roi_by_noise_torch(
                    bg_rois_src, gt_of_bg_rois, iou3d_src, aug_times=aug_times)
                roi_list.append(bg_rois)
                roi_iou_list.append(bg_iou3d)
                roi_gt_list.append(gt_of_bg_rois)

            rois = tf.concat(roi_list, axis=0)
            iou_of_rois = tf.concat(roi_iou_list, axis=0)
            gt_of_rois = tf.concat(roi_gt_list, axis=0)

            batch_rois[idx] = rois
            batch_gt_of_rois[idx] = gt_of_rois
            batch_roi_iou[idx] = iou_of_rois

        return batch_rois, batch_gt_of_rois, batch_roi_iou

    def sample_bg_inds(self, hard_bg_inds, easy_bg_inds,
                       bg_rois_per_this_image):
        if hard_bg_inds.numel() > 0 and easy_bg_inds.numel() > 0:
            hard_bg_rois_num = int(bg_rois_per_this_image * self.hard_bg_ratio)
            easy_bg_rois_num = bg_rois_per_this_image - hard_bg_rois_num

            # sampling hard bg
            rand_idx = tf.constant(np.random.randint(low=0,
                                     high=hard_bg_inds.numel(),
                                     size=(hard_bg_rois_num,)), dtype=tf.int64)
            hard_bg_inds = hard_bg_inds[rand_idx]

            # sampling easy bg
            rand_idx =  tf.constant(np.random.randint(low=0,
                                     high=easy_bg_inds.numel(),
                                     size=(easy_bg_rois_num,)), dtype=tf.int64)
            easy_bg_inds = easy_bg_inds[rand_idx]

            bg_inds = tf.concat([hard_bg_inds, easy_bg_inds], axis=0)
        elif hard_bg_inds.numel() > 0 and easy_bg_inds.numel() == 0:
            hard_bg_rois_num = bg_rois_per_this_image
            # sampling hard bg
            rand_idx = tf.constant(np.random.randint(low=0,
                                     high=hard_bg_inds.numel(),
                                     size=(hard_bg_rois_num,)), dtype=tf.int64)
            bg_inds = hard_bg_inds[rand_idx]
        elif hard_bg_inds.numel() == 0 and easy_bg_inds.numel() > 0:
            easy_bg_rois_num = bg_rois_per_this_image
            # sampling easy bg
            rand_idx = tf.constant(np.random.randint(low=0,
                                     high=easy_bg_inds.numel(),
                                     size=(easy_bg_rois_num,)), dtype=tf.int64)
            bg_inds = easy_bg_inds[rand_idx]
        else:
            raise NotImplementedError

        return bg_inds

    def aug_roi_by_noise_torch(self,
                               roi_boxes3d,
                               gt_boxes3d,
                               iou3d_src,
                               aug_times=10):
        iou_of_rois = tf.zeros(roi_boxes3d.shape[0], dtype=gt_boxes3d.dtype)
        pos_thresh = min(self.reg_fg_thresh, self.cls_fg_thresh)

        for k in range(roi_boxes3d.shape[0]):
            temp_iou = cnt = 0
            roi_box3d = roi_boxes3d[k]

            gt_box3d = gt_boxes3d[k].view(1, 7)
            aug_box3d = roi_box3d
            keep = True
            while temp_iou < pos_thresh and cnt < aug_times:
                if np.random.rand() < 0.2:
                    aug_box3d = roi_box3d  # p=0.2 to keep the original roi box
                    keep = True
                else:
                    aug_box3d = self.random_aug_box3d(roi_box3d)
                    keep = False
                aug_box3d = aug_box3d.view((1, 7))

                iou3d = iou_3d(
                    aug_box3d.detach().cpu().numpy()[:, [0, 1, 2, 5, 3, 4, 6]],
                    gt_box3d.detach().cpu().numpy()[:, [0, 1, 2, 5, 3, 4, 6]])
                iou3d = tf.constant(iou3d)
                temp_iou = iou3d[0][0]
                cnt += 1
            roi_boxes3d[k] = aug_box3d.view(-1)
            if cnt == 0 or keep:
                iou_of_rois[k] = iou3d_src[k]
            else:
                iou_of_rois[k] = temp_iou
        return roi_boxes3d, iou_of_rois

    @staticmethod
    def random_aug_box3d(box3d):
        """
        :param box3d: (7) [x, y, z, h, w, l, ry]
        random shift, scale, orientation
        """
        # pos_range, hwl_range, angle_range, mean_iou
        range_config = [[0.2, 0.1, np.pi / 12,
                         0.7], [0.3, 0.15, np.pi / 12, 0.6],
                        [0.5, 0.15, np.pi / 9,
                         0.5], [0.8, 0.15, np.pi / 6, 0.3],
                        [1.0, 0.15, np.pi / 3, 0.2]]
        idx = tf.constant(np.random.randint(low=0, high=len(range_config), size=(1,))[0], dtype=tf.int64)

        pos_shift = ((tf.rand(3) - 0.5) /
                     0.5) * range_config[idx][0]
        hwl_scale = ((tf.rand(3) - 0.5) /
                     0.5) * range_config[idx][1] + 1.0
        angle_rot = ((tf.rand(1) - 0.5) /
                     0.5) * range_config[idx][2]

        aug_box3d = tf.concat([
            box3d[0:3] + pos_shift, box3d[3:6] * hwl_scale,
            box3d[6:7] + angle_rot
        ],
                              axis=0)
        return aug_box3d

    @staticmethod
    def rotate_pc_along_y_tf(pc, rot_angle):
        """
        :param pc: (N, 512, 3 + C)
        :param rot_angle: (N)
        :return:
        """
        cosa = tf.cos(rot_angle).view(-1, 1)  # (N, 1)
        sina = tf.sin(rot_angle).view(-1, 1)  # (N, 1)

        raw_1 = tf.concat([cosa, -sina], axis=1)  # (N, 2)
        raw_2 = tf.concat([sina, cosa], axis=1)  # (N, 2)
        R = tf.concat((tf.expand_dims(raw_1, axis=1), tf.expand_dims(raw_2, axis=1)),
                      axis=1)  # (N, 2, 2)

        pc_temp = pc[:, :, [0, 2]]  # (N, 512, 2)

        pc[:, :, [0, 2]] = tf.matmul(pc_temp, R.permute(0, 2,
                                                           1))  # (N, 512, 2)

        return pc

    def data_augmentation(self, pts, rois, gt_of_rois):
        """
        :param pts: (B, M, 512, 3)
        :param rois: (B, M. 7)
        :param gt_of_rois: (B, M, 7)
        :return:
        """
        batch_size, boxes_num = pts.shape[0], pts.shape[1]

        # rotation augmentation
        angles = (tf.rand((batch_size, boxes_num)) -
                  0.5 / 0.5) * (np.pi / self.aug_rot_range)

        # calculate gt alpha from gt_of_rois
        temp_x, temp_z, temp_ry = gt_of_rois[:, :,
                                             0], gt_of_rois[:, :,
                                                            2], gt_of_rois[:, :,
                                                                           6]
        temp_beta = tf.atan2(temp_z, temp_x)
        gt_alpha = -tf.sign(
            temp_beta) * np.pi / 2 + temp_beta + temp_ry  # (B, M)

        temp_x, temp_z, temp_ry = rois[:, :, 0], rois[:, :, 2], rois[:, :, 6]
        temp_beta = tf.atan2(temp_z, temp_x)
        roi_alpha = -tf.sign(
            temp_beta) * np.pi / 2 + temp_beta + temp_ry  # (B, M)

        for k in range(batch_size):
            pts[k] = ProposalTargetLayer.rotate_pc_along_y_tf(
                pts[k], angles[k])
            gt_of_rois[k] = tf.squeeze(ProposalTargetLayer.rotate_pc_along_y_tf(
                tf.expand_dims(gt_of_rois[k], axis=1), angles[k]), axis=1)
            rois[k] = tf.squeeze(ProposalTargetLayer.rotate_pc_along_y_tf(
                tf.expand_dims(rois[k], axis=1), angles[k]), axis=1)

            # calculate the ry after rotation
            temp_x, temp_z = gt_of_rois[:, :, 0], gt_of_rois[:, :, 2]
            temp_beta = tf.atan2(temp_z, temp_x)
            gt_of_rois[:, :, 6] = tf.sign(
                temp_beta) * np.pi / 2 + gt_alpha - temp_beta

            temp_x, temp_z = rois[:, :, 0], rois[:, :, 2]
            temp_beta = tf.atan2(temp_z, temp_x)
            rois[:, :,
                 6] = tf.sign(temp_beta) * np.pi / 2 + roi_alpha - temp_beta

        # scaling augmentation
        scales = 1 + ((tf.rand(
            (batch_size, boxes_num)) - 0.5) / 0.5) * 0.05
        pts = pts * tf.expand_dims(tf.expand_dims(scales, axis=2), axis=3)
        gt_of_rois[:, :, 0:6] = gt_of_rois[:, :, 0:6] * tf.expand_dims(scales, axis=2)
        rois[:, :, 0:6] = rois[:, :, 0:6] * tf.expand_dims(scales, axis=2)

        # flip augmentation
        flip_flag = tf.sign(
            tf.rand((batch_size, boxes_num)) - 0.5)
        pts[:, :, :, 0] = pts[:, :, :, 0] * tf.expand_dims(flip_flag, axis=2)
        gt_of_rois[:, :, 0] = gt_of_rois[:, :, 0] * flip_flag
        # flip orientation: ry > 0: pi - ry, ry < 0: -pi - ry
        src_ry = gt_of_rois[:, :, 6]
        ry = (flip_flag == 1).float() * src_ry + (flip_flag == -1).float() * (
            tf.sign(src_ry) * np.pi - src_ry)
        gt_of_rois[:, :, 6] = ry

        rois[:, :, 0] = rois[:, :, 0] * flip_flag
        # flip orientation: ry > 0: pi - ry, ry < 0: -pi - ry
        src_ry = rois[:, :, 6]
        ry = (flip_flag == 1).float() * src_ry + (flip_flag == -1).float() * (
            tf.sign(src_ry) * np.pi - src_ry)
        rois[:, :, 6] = ry

        return pts, rois, gt_of_rois
