"""Class to read and store all the data from the bucky input graph."""
from functools import partial

import networkx as nx

from .adjmat import buckyAij
from .numerical_libs import reimport_numerical_libs, xp
from .util.cached_prop import cached_property
from .util.rolling_mean import rolling_mean


class buckyGraphData:
    """Contains and preprocesses all the data imported from an input graph file."""

    def __init__(self, G, sparse=True):

        reimport_numerical_libs()

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

        self.Aij = buckyAij(G, sparse, a_min=0.0)

        # TODO move these params to config?
        self._rolling_mean_type = "arithmetic"  # "geometric"
        self._rolling_mean_window_size = 7
        self.rolling_mean_func_cum = partial(
            rolling_mean,
            window_size=self._rolling_mean_window_size,
            axis=0,
            mean_type=self._rolling_mean_type,
        )
        self.rolling_mean_func_inc = partial(
            rolling_mean,
            window_size=self._rolling_mean_window_size,
            axis=0,
            mean_type="arithmetic",
        )

    # TODO maybe provide a decorator or take a lambda or something to generalize it?
    # also this would be good if it supported rolling up to adm0 for multiple countries
    # memo so we don'y have to handle caching this on the input data?
    def sum_adm1(self, adm2_arr):
        """Return the adm1 sum of a variable defined at the adm2 level using the mapping on the graphi."""
        # TODO add in axis param, we call this a bunch on array.T
        # assumes 1st dim is adm2 indexes
        shp = (self.max_adm1 + 1,) + adm2_arr.shape[1:]
        out = xp.zeros(shp, dtype=adm2_arr.dtype)
        xp.scatter_add(out, self.adm1_id, adm2_arr)
        return out

    # TODO add scatter_adm2 with weights. Noone should need to check self.adm1/2_id outside this class

    # TODO other adm1 reductions (like harmonic mean), also add weights (for things like Nj)

    @cached_property
    def rolling_inc_cases(self):
        """Return the rolling mean of incident cases."""
        # return self.rolling_mean_func_inc(self.inc_case_hist)
        return xp.diff(self.rolling_cum_cases, axis=0)

    @cached_property
    def rolling_inc_deaths(self):
        """Return the rolling mean of incident deaths."""
        # return self.rolling_mean_func_inc(self.inc_death_hist)
        return xp.diff(self.rolling_cum_deaths, axis=0)

    @cached_property
    def rolling_cum_cases(self):
        """Return the rolling mean of cumulative cases."""
        return self.rolling_mean_func_cum(self.cum_case_hist)

    @cached_property
    def rolling_cum_deaths(self):
        """Return the rolling mean of cumulative deaths."""
        return self.rolling_mean_func_cum(self.cum_death_hist)


def _read_node_attr(G, name, diff=False, dtype=float, a_min=None, a_max=None):
    """Read an attribute from every node into a cupy/numpy array and optionally clip and/or diff it."""
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
