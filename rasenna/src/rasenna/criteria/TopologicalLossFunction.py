import numpy as np
import torch.nn as nn
import torch
import time
import math
from inferno.extensions.criteria.set_similarity_measures import SorensenDiceLoss
#from PersistencePython import cubePers
from torch.autograd import Function

import imp
imp.load_dynamic('PersistencePython', '/export/home/jgrieser/anaconda3/envs/segmFr/lib/PersistencePython.so')

class TopologicalLossFunction(Function):

    @staticmethod
    def forward(ctx, input, target):
        """
        compute topological loss
        """
        input_dgms, input_birth_cp, input_death_cp = compute_persistence_2DImg(input.cpu().detach(), dimension=1)
        target_dgms, target_birth_cp, target_death_cp = compute_persistence_2DImg(target.cpu().detach(), dimension=1)

        ctx.save_for_backward(input_dgms, input_birth_cp, input_death_cp, target_dgms)

        lh_dgm = input_dgms.numpy()
        gt_dgm = target_dgms.numpy()

        force_list, idx_holes_to_fix, idx_holes_to_remove = compute_dgm_force(lh_dgm, gt_dgm)
        loss = 0.0
        for idx in idx_holes_to_fix:
            loss = loss + force_list[idx, 0] ** 2 + force_list[idx, 1] ** 2
        for idx in idx_holes_to_remove:
            loss = loss + force_list[idx, 0] ** 2 + force_list[idx, 1] ** 2
        return torch.tensor(loss)

    @staticmethod
    def backward(ctx, grad_output):
        print('Backwards...')

        lh_dgm, lh_bcp, lh_dcp, gt_dgm = ctx.saved_tensors
        """
        compute topological gradient
        """
        force_list, idx_holes_to_fix, idx_holes_to_remove = compute_dgm_force(lh_dgm, gt_dgm)

        # each birth/death crit pt of a persistence dot to move corresponds to a row
        # each row has 3 values: x, y coordinates, and the force (increase/decrease)
        topo_grad = np.zeros([2 * (len(idx_holes_to_fix) + len(idx_holes_to_remove)), 3])

        counter = 0
        for idx in idx_holes_to_fix:
            topo_grad[counter] = [lh_bcp[idx, 1], lh_bcp[idx, 0], force_list[idx, 0]]
            counter = counter + 1
            topo_grad[counter] = [lh_dcp[idx, 1], lh_dcp[idx, 0], force_list[idx, 1]]
            counter = counter + 1
        for idx in idx_holes_to_remove:
            topo_grad[counter] = [lh_bcp[idx, 1], lh_bcp[idx, 0], force_list[idx, 0]]
            counter = counter + 1
            topo_grad[counter] = [lh_dcp[idx, 1], lh_dcp[idx, 0], force_list[idx, 1]]
            counter = counter + 1

        topo_grad[:, 2] = topo_grad[:, 2] * -2

        print('Topological gradient: ', topo_grad)

        return torch.from_numpy(topo_grad).cuda()

def compute_persistence_2DImg(f, dimension):
    """
    compute persistence diagram in a 2D function (can be N-dim) and critical pts
    only generate 1D homology dots and critical points
    """
    assert len(f.shape) == 2  # f has to be 2D function
    dim = 2

    # pad the function with a few pixels of minimum values
    # this way one can compute the 1D topology as loops
    # remember to transform back to the original coordinates when finished
    padwidth = 2
    padvalue = min(f.min(), 0.0)
    f_padded = np.pad(f, padwidth, 'constant', constant_values=padvalue)

    # call persistence code to compute diagrams
    # loads PersistencePython.so (compiled from C++); should be in current dir
    from PersistencePython import cubePers
    persistence_result = cubePers(np.reshape(
        f_padded, f_padded.size).tolist(), list(f_padded.shape), 0.001)

    # only take 1-dim topology, first column of persistence_result is dimension
    persistence_result_filtered = np.array(list(filter(lambda x: x[0] == float(dimension), persistence_result)))

    # persistence diagram (second and third columns are coordinates)
    dgm = persistence_result_filtered[:, 1:3]

    # critical points
    birth_cp_list = persistence_result_filtered[:, 4:4 + dim]
    death_cp_list = persistence_result_filtered[:, 4 + dim:]

    # when mapping back, shift critical points back to the original coordinates
    birth_cp_list = birth_cp_list - padwidth
    death_cp_list = death_cp_list - padwidth

    return torch.from_numpy(dgm), torch.from_numpy(birth_cp_list), torch.from_numpy(death_cp_list)

def compute_dgm_force(lh_dgm, gt_dgm):
    # get persistence list from both diagrams
    lh_pers = lh_dgm[:, 1] - lh_dgm[:, 0]
    gt_pers = gt_dgm[:, 1] - gt_dgm[:, 0]

    # more lh dots than gt dots
    assert lh_pers.size > gt_pers.size

    # check to ensure that all gt dots have persistence 1
    tmp = gt_pers > 0.999
    # assert tmp.sum() == gt_pers.size

    gt_n_holes = gt_pers.size  # number of holes in gt

    # get "perfect holes" - holes which do not need to be fixed, i.e., find top
    # lh_n_holes_perfect indices
    # check to ensure that at least one dot has persistence 1; it is the hole
    # formed by the padded boundary
    # if no hole is ~1 (ie >.999) then just take all holes with max values
    tmp = lh_pers > 0.999  # old: assert tmp.sum() >= 1
    if tmp.sum() >= 1:
        # n_holes_to_fix = gt_n_holes - lh_n_holes_perfect
        lh_n_holes_perfect = tmp.sum()
        idx_holes_perfect = np.argpartition(lh_pers, -lh_n_holes_perfect)[
                            -lh_n_holes_perfect:]
    else:
        idx_holes_perfect = np.where(lh_pers == lh_pers.max())[0]

    # find top gt_n_holes indices
    idx_holes_to_fix_or_perfect = np.argpartition(lh_pers, -gt_n_holes)[
                                  -gt_n_holes:]

    # the difference is holes to be fixed to perfect
    idx_holes_to_fix = list(
        set(idx_holes_to_fix_or_perfect) - set(idx_holes_perfect))

    # remaining holes are all to be removed
    idx_holes_to_remove = list(
        set(range(lh_pers.size)) - set(idx_holes_to_fix_or_perfect))

    # only select the ones whose persistence is large enough
    # set a threshold to remove meaningless persistence dots
    # TODO values below this are small dents so dont fix them; tune this value?
    pers_thd = 0.03
    idx_valid = np.where(lh_pers > pers_thd)[0]
    idx_holes_to_remove = list(
        set(idx_holes_to_remove).intersection(set(idx_valid)))

    force_list = np.zeros(lh_dgm.shape)
    # push each hole-to-fix to (0,1)
    force_list[idx_holes_to_fix, 0] = 0 - lh_dgm[idx_holes_to_fix, 0]
    force_list[idx_holes_to_fix, 1] = 1 - lh_dgm[idx_holes_to_fix, 1]

    # push each hole-to-remove to (0,1)
    force_list[idx_holes_to_remove, 0] = lh_pers[idx_holes_to_remove] / \
                                         math.sqrt(2.0)
    force_list[idx_holes_to_remove, 1] = -lh_pers[idx_holes_to_remove] / \
                                         math.sqrt(2.0)

    return force_list, idx_holes_to_fix, idx_holes_to_remove