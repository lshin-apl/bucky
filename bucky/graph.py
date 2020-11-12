import logging

import networkx as nx

from .adjmat import buckyAij

xp = None


class buckyGraphData:
    def __init__(self, G, sparse=True):
        # TODO maybe we should make the reimport a functoin in num_libs?
        global xp, xp_sparse
        if xp is None:
            from . import xp

        G = nx.convert_node_labels_to_integers(G)
        self.cum_case_hist, self.inc_case_hist = _read_node_attr(G, "case_hist", diff=True, a_min=0.0)
        self.cum_death_hist, self.inc_death_hist = _read_node_attr(G, "death_hist", diff=True, a_min=0.0)
        self.Nij = _read_node_attr(G, "N_age_init", a_min=1e-5)
        self.Nj = xp.sum(self.Nij, axis=0)

        # TODO add adm0 to support multiple countries
        self.adm2_id = _read_node_attr(G, G.graph["adm2_key"], dtype=int)[0]
        self.adm1_id = _read_node_attr(G, G.graph["adm1_key"], dtype=int)[0]

        # in case we want to alloc something indexed by adm1/2
        self.max_adm2 = xp.to_cpu(xp.max(self.adm2_id))
        self.max_adm1 = xp.to_cpu(xp.max(self.adm1_id))

        # self.Aij, self.A_diag = _read_edge_mat(G, sparse=sparse)
        self.Aij = buckyAij(G, sparse)

    # TODO maybe provide a decorator or take a lambda or something to generalize it?
    # also this would be good if it supported rolling up to adm0 for multiple countries
    # memo so we don'y have to handle caching this on the input data?
    def sum_adm1(self, adm2_arr):
        # TODO add in axis param, we call this a bunch on array.T
        # assumes 1st dim is adm2 indexes
        shp = (self.max_adm1 + 1,) + adm2_arr.shape[1:]
        out = xp.zeros(shp, dtype=adm2_arr.dtype)
        xp.scatter_add(out, self.adm1_id, adm2_arr)
        return out


# @staticmethod
def _read_node_attr(G, name, diff=False, dtype=float, a_min=None, a_max=None):
    clipping = (a_min is not None) or (a_max is not None)
    node_list = list(nx.get_node_attributes(G, name).values())
    arr = xp.vstack(node_list).astype(dtype).T
    if clipping:
        arr = xp.clip(arr, a_min=a_min, a_max=a_max)

    if diff:
        arr_diff = xp.diff(arr, axis=0).astype(dtype)
        if clipping:
            arr_diff = xp.clip(arr_diff, a_min=a_min, a_max=a_max)
        return arr, arr_diff

    return arr


# @staticmethod
# def _read_edge_mat(G, weight_attr="weight", sparse=True):
#    edges = xp.array(list(G.edges(data=weight_attr))).T
#    A = xp_sparse.coo_matrix((edges[2], (edges[0].astype(int), edges[1].astype(int))))
#    A = A.tocsr()  # just b/c it will do this for almost every op on the array anyway...
#    if not sparse:
#        A = A.toarray()
#    A_diag = edges[2][edges[0] == edges[1]]
#    return A, A_diag

# @staticmethod
def _mat_norm(mat, axis=0):
    mat_norm = 1.0 / mat.sum(axis=axis)  # this returns a np.matrix if mat is scipy.sparse
    mat_norm = xp.array(A_norm)
    # TODO check type of mat (if sparse we handle differently)
