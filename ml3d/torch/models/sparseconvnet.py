import numpy as np
import torch
import torch.nn as nn
import pdb
import math

from .base_model import BaseModel
from ...utils import MODEL
from ..modules.losses import filter_valid_label
from ...datasets.utils import trans_augment
from ...datasets.augment import SemsegAugmentation
from open3d.ml.torch.layers import SparseConv, SparseConvTranspose
from open3d.ml.torch.ops import voxelize, reduce_subarrays_sum


class SparseConvUnet(BaseModel):

    def __init__(
            self,
            name="SparseConvUnet",
            device="cuda",
            m=16,
            voxel_size=0.05,
            reps=1,  # Conv block repetitions.
            residual_blocks=False,
            in_channels=3,
            num_classes=20,
            **kwargs):
        super(SparseConvUnet, self).__init__(name=name,
                                             device=device,
                                             m=m,
                                             voxel_size=voxel_size,
                                             reps=reps,
                                             residual_blocks=residual_blocks,
                                             in_channels=in_channels,
                                             num_classes=num_classes,
                                             **kwargs)
        cfg = self.cfg
        self.device = device
        self.augment = SemsegAugmentation(cfg.augment)
        self.m = cfg.m
        self.inp = InputLayer()
        self.ssc = SubmanifoldSparseConv(in_channels=in_channels,
                                         filters=m,
                                         kernel_size=[3, 3, 3])
        self.unet = UNet(reps, [m, 2 * m, 3 * m, 4 * m, 5 * m, 6 * m, 7 * m],
                         residual_blocks)
        self.bn = nn.BatchNorm1d(m, eps=1e-4, momentum=0.01)
        self.relu = nn.ReLU()
        self.linear = nn.Linear(m, num_classes)
        self.out = OutputLayer()

    def forward(self, inputs):
        output = []
        start_idx = 0
        for length in inputs.batch_lengths:
            pos = inputs.point[start_idx:start_idx + length]
            feat = inputs.feat[start_idx:start_idx + length]

            feat, pos, rev = self.inp(feat, pos)
            feat = self.ssc(feat, pos, voxel_size=1.0)
            feat = self.unet(pos, feat)
            feat = self.bn(feat)
            feat = self.relu(feat)
            feat = self.linear(feat)
            feat = self.out(feat, rev)

            output.append(feat)
            start_idx += length
        return torch.cat(output, 0)

    def preprocess(self, data, attr):
        points = np.array(data['point'], dtype=np.float32)

        if 'label' not in data.keys() or data['label'] is None:
            labels = np.zeros((points.shape[0],), dtype=np.int32)
        else:
            labels = np.array(data['label'], dtype=np.int32).reshape((-1,))

        if 'feat' not in data.keys() or data['feat'] is None:
            raise Exception(
                "SparseConvnet doesn't work without feature values.")

        feat = np.array(data['feat'], dtype=np.float32)
        scale = 1. / self.cfg.voxel_size

        if attr['split'] in ['training', 'train']:
            m = np.eye(3) + np.random.randn(3, 3) * 0.1
            m[0][0] *= np.random.randint(0, 2) * 2 - 1
            m *= scale
            theta = np.random.rand() * 2 * math.pi
            m = np.matmul(
                m, [[math.cos(theta), math.sin(theta), 0],
                    [-math.sin(theta), math.cos(theta), 0], [0, 0, 1]])
            points = np.matmul(points, m)

            feat = feat + np.random.normal(0, 1, 3) * 0.1

        else:
            m = np.eye(3)
            m[0][0] *= np.random.randint(0, 2) * 2 - 1
            m *= scale
            theta = np.random.rand() * 2 * math.pi
            m = np.matmul(
                m, [[math.cos(theta), math.sin(theta), 0],
                    [-math.sin(theta), math.cos(theta), 0], [0, 0, 1]])
            points = np.matmul(points, m) + 4096 / 2 + np.random.uniform(
                -2, 2, 3)

        m = points.min(0)
        M = points.max(0)
        offset = -m + np.clip(4096 - M + m - 0.001, 0, None) * np.random.rand(
            3) + np.clip(4096 - M + m + 0.001, None, 0) * np.random.rand(3)

        points += offset
        idxs = (points.min(1) >= 0) * (points.max(1) < 4096)

        points = points[idxs]
        feat = feat[idxs]
        labels = labels[idxs]

        points = (points.astype(np.int32) + 0.5).astype(
            np.float32)  # Move points to voxel center.

        data = {}
        data['point'] = points
        data['feat'] = feat
        data['label'] = labels

        return data

    def transform(self, data, attr):
        device = self.device
        data['point'] = torch.from_numpy(data['point'])
        data['feat'] = torch.from_numpy(data['feat'])
        data['label'] = torch.from_numpy(data['label'])

        return data

    def inference_begin(self, data):
        data = self.preprocess(data, {'split': 'test'})
        data['batch_lengths'] = [data['point'].shape[0]]
        data = self.transform(data, {})

        self.inference_input = data

    def inference_preprocess(self):
        return self.inference_input

    def inference_end(self, inputs, results):
        results = torch.reshape(results, (-1, self.cfg.num_classes))

        m_softmax = torch.nn.Softmax(dim=-1)
        results = m_softmax(results)
        results = results.cpu().data.numpy()

        probs = np.reshape(results, [-1, self.cfg.num_classes])

        pred_l = np.argmax(probs, 1)

        return {'inference_labels': pred_l, 'inference_scores': probs}

    def get_loss(self, Loss, results, inputs, device):
        """
        Runs the loss on outputs of the model
        :param outputs: logits
        :param labels: labels
        :return: loss
        """
        cfg = self.cfg
        labels = inputs['data'].label

        scores, labels = filter_valid_label(results, labels, cfg.num_classes,
                                            cfg.ignored_label_inds, device)

        loss = Loss.weighted_CrossEntropyLoss(scores, labels)

        return loss, labels, scores

    def get_optimizer(self, cfg_pipeline):
        optimizer = torch.optim.Adam(self.parameters(), lr=cfg_pipeline.adam_lr)
        scheduler = torch.optim.lr_scheduler.ExponentialLR(
            optimizer, cfg_pipeline.scheduler_gamma)

        return optimizer, scheduler


MODEL._register_module(SparseConvUnet, 'torch')


class InputLayer(nn.Module):

    def __init__(self, voxel_size=1.0):
        super(InputLayer, self).__init__()
        self.voxel_size = torch.Tensor([voxel_size, voxel_size, voxel_size])

    def forward(self, features, inp_positions):
        v = voxelize(inp_positions, self.voxel_size, torch.Tensor([0, 0, 0]),
                     torch.Tensor([40960, 40960, 40960]))

        # Contiguous repeating positions.
        inp_positions = inp_positions[v.voxel_point_indices]
        features = features[v.voxel_point_indices]

        # Find reverse mapping.
        rev1 = np.zeros((inp_positions.shape[0],))
        rev1[v.voxel_point_indices.cpu().numpy()] = np.arange(
            inp_positions.shape[0])
        rev1 = rev1.astype(np.int32)

        # Unique positions.
        inp_positions = inp_positions[v.voxel_point_row_splits[:-1]]

        # Mean of features.
        count = v.voxel_point_row_splits[1:] - v.voxel_point_row_splits[:-1]
        rev2 = np.repeat(np.arange(count.shape[0]),
                         count.cpu().numpy()).astype(np.int32)

        features_avg = inp_positions.clone()
        features_avg[:, 0] = reduce_subarrays_sum(features[:, 0],
                                                  v.voxel_point_row_splits)
        features_avg[:, 1] = reduce_subarrays_sum(features[:, 1],
                                                  v.voxel_point_row_splits)
        features_avg[:, 2] = reduce_subarrays_sum(features[:, 2],
                                                  v.voxel_point_row_splits)

        features_avg = features_avg / count.unsqueeze(1)

        return features_avg, inp_positions, rev2[rev1]


class OutputLayer(nn.Module):

    def __init__(self, voxel_size=1.0):
        super(OutputLayer, self).__init__()

    def forward(self, features, rev):
        return features[rev]


class SubmanifoldSparseConv(nn.Module):

    def __init__(self,
                 in_channels,
                 filters,
                 kernel_size,
                 use_bias=False,
                 offset=None,
                 normalize=False):
        super(SubmanifoldSparseConv, self).__init__()

        if offset is None:
            if kernel_size[0] % 2:
                offset = 0.
            else:
                offset = 0.5

        offset = torch.full((3,), offset, dtype=torch.float32)
        self.net = SparseConv(in_channels=in_channels,
                              filters=filters,
                              kernel_size=kernel_size,
                              use_bias=use_bias,
                              offset=offset,
                              normalize=normalize)

    def forward(self,
                features,
                inp_positions,
                out_positions=None,
                voxel_size=1.0):
        if out_positions is None:
            out_positions = inp_positions
        return self.net(features, inp_positions, out_positions, voxel_size)

    def __name__(self):
        return "SubmanifoldSparseConv"


def calculate_grid(inp_positions):
    filter = torch.Tensor([[-1, -1, -1], [-1, -1, 0], [-1, 0, -1], [-1, 0, 0],
                           [0, -1, -1], [0, -1, 0], [0, 0, -1],
                           [0, 0, 0]]).to(inp_positions.device)

    out_pos = inp_positions.long().repeat(1, filter.shape[0]).reshape(-1, 3)
    filter = filter.repeat(inp_positions.shape[0], 1)

    out_pos = out_pos + filter
    out_pos = out_pos[out_pos.min(1).values >= 0]
    out_pos = out_pos[(~((out_pos.long() % 2).bool()).any(1))]
    out_pos = torch.unique(out_pos, dim=0)

    return out_pos + 0.5


class Convolution(nn.Module):

    def __init__(self,
                 in_channels,
                 filters,
                 kernel_size,
                 use_bias=False,
                 offset=None,
                 normalize=False):
        super(Convolution, self).__init__()

        if offset is None:
            if kernel_size[0] % 2:
                offset = 0.
            else:
                offset = -0.5

        offset = torch.full((3,), offset, dtype=torch.float32)
        self.net = SparseConv(in_channels=in_channels,
                              filters=filters,
                              kernel_size=kernel_size,
                              use_bias=use_bias,
                              offset=offset,
                              normalize=normalize)

    def forward(self, features, inp_positions, voxel_size=1.0):
        out_positions = calculate_grid(inp_positions)
        out = self.net(features, inp_positions, out_positions, voxel_size)
        return out, out_positions / 2

    def __name__(self):
        return "Convolution"


class DeConvolution(nn.Module):

    def __init__(self,
                 in_channels,
                 filters,
                 kernel_size,
                 use_bias=False,
                 offset=None,
                 normalize=False):
        super(DeConvolution, self).__init__()

        if offset is None:
            if kernel_size[0] % 2:
                offset = 0.
            else:
                offset = -0.5

        offset = torch.full((3,), offset, dtype=torch.float32)
        self.net = SparseConvTranspose(in_channels=in_channels,
                                       filters=filters,
                                       kernel_size=kernel_size,
                                       use_bias=use_bias,
                                       offset=offset,
                                       normalize=normalize)

    def forward(self, features, inp_positions, out_positions, voxel_size=1.0):
        return self.net(features, inp_positions, out_positions, voxel_size)

    def __name__(self):
        return "DeConvolution"


class ConcatFeat(nn.Module):

    def __init__(self):
        super(ConcatFeat, self).__init__()

    def __name__(self):
        return "ConcatFeat"

    def forward(self, feat):
        return feat


class JoinFeat(nn.Module):

    def __init__(self):
        super(JoinFeat, self).__init__()

    def __name__(self):
        return "JoinFeat"

    def forward(self, feat_cat, feat):
        return torch.cat([feat_cat, feat], -1)


class NetworkInNetwork(nn.Module):

    def __init__(self, nIn, nOut, bias=False):
        super(NetworkInNetwork, self).__init__()
        if nIn == nOut:
            self.linear = nn.Identity()
        else:
            self.linear = nn.Linear(nIn, nOut, bias=bias)

    def forward(self, inputs):
        return self.linear(inputs)


class ResidualBlock(nn.Module):

    def __init__(self, nIn, nOut):
        super(ResidualBlock, self).__init__()

        self.lin = NetworkInNetwork(nIn, nOut)

        self.bn1 = nn.BatchNorm1d(nIn, eps=1e-4, momentum=0.01)
        self.relu1 = nn.LeakyReLU(0)
        self.scn1 = SubmanifoldSparseConv(in_channels=nIn,
                                          filters=nOut,
                                          kernel_size=[3, 3, 3])

        self.bn2 = nn.BatchNorm1d(nOut, eps=1e-4, momentum=0.01)
        self.relu2 = nn.LeakyReLU(0)
        self.scn2 = SubmanifoldSparseConv(in_channels=nOut,
                                          filters=nOut,
                                          kernel_size=[3, 3, 3])

    def forward(self, feat, pos):
        out1 = self.lin(feat)

        if feat.shape[0] < 3:
            feat = ((feat - self.bn1.running_mean) /
                    torch.sqrt(self.bn1.running_var +
                               self.bn1.eps)) * self.bn1.weight + self.bn1.bias
        else:
            feat = self.bn1(feat)

        feat = self.relu1(feat)

        feat = self.scn1(feat, pos)

        if feat.shape[0] < 3:
            feat = ((feat - self.bn2.running_mean) /
                    torch.sqrt(self.bn2.running_var +
                               self.bn2.eps)) * self.bn2.weight + self.bn2.bias
        else:
            feat = self.bn2(feat)
        feat = self.relu2(feat)

        out2 = self.scn2(feat, pos)

        return out1 + out2

    def __name__(self):
        return "ResidualBlock"


class UNet(nn.Module):

    def __init__(self,
                 reps,
                 nPlanes,
                 residual_blocks=False,
                 downsample=[2, 2],
                 leakiness=0):
        super(UNet, self).__init__()
        self.net = nn.ModuleList(self.U(nPlanes, residual_blocks, reps))
        self.residual_blocks = residual_blocks

    @staticmethod
    def block(m, a, b, residual_blocks):
        if residual_blocks:
            m.append(ResidualBlock(a, b))

        else:
            m.append(nn.BatchNorm1d(a, eps=1e-4, momentum=0.01))
            m.append(nn.LeakyReLU(0))
            m.append(
                SubmanifoldSparseConv(in_channels=a,
                                      filters=b,
                                      kernel_size=[3, 3, 3]))

    @staticmethod
    def U(nPlanes, residual_blocks, reps):
        m = []
        for i in range(reps):
            UNet.block(m, nPlanes[0], nPlanes[0], residual_blocks)

        if len(nPlanes) > 1:
            m.append(ConcatFeat())
            m.append(nn.BatchNorm1d(nPlanes[0], eps=1e-4, momentum=0.01))
            m.append(nn.LeakyReLU(0))
            m.append(
                Convolution(in_channels=nPlanes[0],
                            filters=nPlanes[1],
                            kernel_size=[2, 2, 2]))
            m = m + UNet.U(nPlanes[1:], residual_blocks, reps)
            m.append(nn.BatchNorm1d(nPlanes[1], eps=1e-4, momentum=0.01))
            m.append(nn.LeakyReLU(0))
            m.append(
                DeConvolution(in_channels=nPlanes[1],
                              filters=nPlanes[0],
                              kernel_size=[2, 2, 2]))

            m.append(JoinFeat())

            for i in range(reps):
                UNet.block(m, nPlanes[0] * (2 if i == 0 else 1), nPlanes[0],
                           residual_blocks)

        return m

    def forward(self, pos, feat):
        conv_pos = []
        concat_feat = []
        for module in self.net:
            if isinstance(module, nn.BatchNorm1d):
                if feat.shape[0] < 3:
                    # Cannot calculate std_dev for dimension 1, using running statistics.
                    feat = ((feat - module.running_mean) /
                            torch.sqrt(module.running_var + module.eps)
                           ) * module.weight + module.bias
                else:
                    feat = module(feat)
            elif isinstance(module, nn.LeakyReLU):
                feat = module(feat)

            elif module.__name__() == "ResidualBlock":
                feat = module(feat, pos)

            elif module.__name__() == "SubmanifoldSparseConv":
                feat = module(feat, pos)

            elif module.__name__() == "Convolution":
                conv_pos.append(pos.clone())
                feat, pos = module(feat, pos)
            elif module.__name__() == "DeConvolution":
                feat = module(feat, 2 * pos, conv_pos[-1])
                pos = conv_pos.pop()

            elif module.__name__() == "ConcatFeat":
                concat_feat.append(module(feat).clone())
            elif module.__name__() == "JoinFeat":
                feat = module(concat_feat.pop(), feat)

            else:
                raise Exception("Unknown module {}".format(module))

        return feat


def load_unet_wts(net, path):
    wts = list(torch.load(path).values())
    state_dict = net.state_dict()
    i = 0
    for key in state_dict.keys():
        if 'offset' in key or 'tracked' in key:
            continue
        if len(wts[i].shape) == 4:
            shp = wts[i].shape
            state_dict[key] = np.transpose(
                wts[i].reshape(int(shp[0]**(1 / 3)), int(shp[0]**(1 / 3)),
                               int(shp[0]**(1 / 3)), shp[-2], shp[-1]),
                (2, 1, 0, 3, 4))
        else:
            state_dict[key] = wts[i]
        i += 1

    net.load_state_dict(state_dict)
