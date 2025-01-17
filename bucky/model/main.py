"""The main module handling the simulation"""
import copy
import datetime
import logging
import os
import pickle
import queue
import random
import sys
import threading
import warnings
from functools import lru_cache
from pprint import pformat  # TODO set some defaults for width/etc with partial?

import networkx as nx
import numpy as np
import pandas as pd
import tqdm

from ..numerical_libs import reimport_numerical_libs, use_cupy, xp, xp_ivp
from ..util.distributions import approx_mPERT_sample, truncnorm
from ..util.util import TqdmLoggingHandler, _banner
from .arg_parser_model import parser
from .estimation import estimate_doubling_time, estimate_Rt
from .graph import buckyGraphData
from .npi import get_npi_params
from .parameters import buckyParams
from .state import buckyState

# supress pandas warning caused by pyarrow
warnings.simplefilter(action="ignore", category=FutureWarning)
# TODO we do alot of allowing div by 0 and then checking for nans later, we should probably refactor that
warnings.simplefilter(action="ignore", category=RuntimeWarning)

# TODO move to a new file and add some more exception types
class SimulationException(Exception):
    """A generic exception to throw when there's an error related to the simulation"""

    pass  # pylint: disable=unnecessary-pass


@lru_cache(maxsize=None)
def get_runid():  # TODO move to util and rename to timeid or something
    """Gets a UUID based of the current datatime and caches it"""
    dt_now = datetime.datetime.now()
    return str(dt_now).replace(" ", "__").replace(":", "_").split(".")[0]


def frac_last_n_vals(arr, n, axis=0, offset=0):  # TODO assumes come from end of array currently, move to util
    """Return the last n values along an axis of an array; if n is a float, include the fractional amount of the int(n)-1 element"""
    int_slice_ind = (
        [slice(None)] * (axis)
        + [slice(-int(n + offset), -int(xp.ceil(offset)) or None)]
        + [slice(None)] * (arr.ndim - axis - 1)
    )
    ret = arr[int_slice_ind]
    # handle fractional element before the standard slice
    if (n + offset) % 1:
        frac_slice_ind = (
            [slice(None)] * (axis)
            + [slice(-int(n + offset + 1), -int(n + offset))]
            + [slice(None)] * (arr.ndim - axis - 1)
        )
        ret = xp.concatenate((((n + offset) % 1) * arr[frac_slice_ind], ret), axis=axis)
    # handle fractional element after the standard slice
    if offset % 1:
        frac_slice_ind = (
            [slice(None)] * (axis)
            + [slice(-int(offset + 1), -int(offset) or None)]
            + [slice(None)] * (arr.ndim - axis - 1)
        )
        ret = xp.concatenate((ret, (1.0 - (offset % 1)) * arr[frac_slice_ind]), axis=axis)

    return ret


class buckyModelCovid:
    """Class that handles one full simulation (both time integration and managing MC states)"""

    def __init__(
        self,
        debug=False,
        sparse_aij=False,
        t_max=None,
        graph_file=None,
        par_file=None,
        npi_file=None,
        disable_npi=False,
        reject_runs=False,
    ):
        """Initialize the class, do some bookkeeping and read in the input graph"""
        self.debug = debug
        self.sparse = sparse_aij  # we can default to none and autodetect
        # w/ override (maybe when #adm2 > 5k and some sparsity critera?)

        # Integrator params
        self.dt = 1.0  # time step for model output (the internal step is adaptive...)
        self.t_max = t_max
        self.run_id = get_runid()
        logging.info(f"Run ID: {self.run_id}")

        self.npi_file = npi_file
        self.disable_npi = disable_npi
        self.reject_runs = reject_runs

        self.output_dates = None

        # COVID/model params from par file
        self.bucky_params = buckyParams(par_file)
        self.consts = self.bucky_params.consts
        self.dists = self.bucky_params.dists

        self.g_data = self.load_graph(graph_file)

    def update_params(self, update_dict):
        self.bucky_params.update_params(update_dict)
        self.consts = self.bucky_params.consts
        self.dists = self.bucky_params.dists

    def load_graph(self, graph_file):
        """Load the graph data and calculate all the variables that are static across MC runs"""
        # TODO refactor to just ahve this return g_data
        # (it's currently the code block that used to be at the top of reset)

        logging.info("loading graph")
        with open(graph_file, "rb") as f:
            G = pickle.load(f)  # nosec

        # Load data from input graph
        # TODO we should go through an replace lots of math using self.g_data.* with function IN buckyGraphData
        g_data = buckyGraphData(G, self.sparse)

        """
        if "IFR" in G.nodes[list(G.nodes.keys())[0]]:
            logging.info("Using ifr from graph")
            self.use_G_ifr = True
            node_IFR = nx.get_node_attributes(G, "IFR")
            self.ifr = xp.asarray((np.vstack(list(node_IFR.values()))).T)
        else:
            self.use_G_ifr = False
        """

        # Make contact mats sym and normalized
        self.contact_mats = G.graph["contact_mats"]
        if self.debug:
            logging.debug(f"graph contact mats: {G.graph['contact_mats'].keys()}")
        for mat in self.contact_mats:
            c_mat = xp.array(self.contact_mats[mat])
            c_mat = (c_mat + c_mat.T) / 2.0
            self.contact_mats[mat] = c_mat
        # remove all_locations so we can sum over the them ourselves
        if "all_locations" in self.contact_mats:
            del self.contact_mats["all_locations"]

        # Remove unknown contact mats
        valid_contact_mats = ["home", "work", "other_locations", "school"]
        self.contact_mats = {k: v for k, v in self.contact_mats.items() if k in valid_contact_mats}

        self.Cij = xp.vstack([self.contact_mats[k][None, ...] for k in sorted(self.contact_mats)])

        # Get stratified population (and total)
        self.Nij = g_data.Nij
        self.Nj = g_data.Nj
        self.n_age_grps = self.Nij.shape[0]  # TODO factor out

        n_nodes = self.Nij.shape[-1]  # TODO factor out

        self.first_date = datetime.date.fromisoformat(G.graph["start_date"])

        # fill in npi_params either from file or as ones
        self.npi_params = get_npi_params(g_data, self.first_date, self.t_max, self.npi_file, self.disable_npi)

        if self.npi_params["npi_active"]:
            self.Cij = xp.broadcast_to(self.Cij, (n_nodes,) + self.Cij.shape)
            self.npi_params["contact_weights"] = self.npi_params["contact_weights"][..., None, None]
        else:
            self.Cij = xp.sum(self.Cij, axis=0)
            self.Cij = (self.Cij + self.Cij.T) / 2.0
            self.Cij = self.Cij / xp.sum(self.Cij, axis=1)

        self.adm0_cfr_reported = None
        self.adm1_cfr_reported = None
        self.adm2_cfr_reported = None

        # If HHS hospitalization data is on the graph, use it to rescale initial H counts and CHR
        self.rescale_chr = "hhs_data" in G.graph
        if self.rescale_chr:
            self.adm1_current_hosp = xp.zeros((g_data.max_adm1 + 1,), dtype=float)
            hhs_data = G.graph["hhs_data"].reset_index()
            hhs_data = (
                hhs_data.set_index("date")
                .sort_index()
                .groupby("adm1")
                .rolling(7)  # , center=True)
                .mean()
                .drop(columns="adm1")
                .reset_index()
            )
            hhs_curr_data = hhs_data.loc[hhs_data.date == str(self.first_date)]
            hhs_curr_data = hhs_curr_data.set_index("adm1").sort_index()
            tot_hosps = (
                hhs_curr_data.total_adult_patients_hospitalized_confirmed_covid
                + hhs_curr_data.total_pediatric_patients_hospitalized_confirmed_covid
            )
            self.adm1_current_hosp[tot_hosps.index.to_numpy()] = tot_hosps.to_numpy()
            if self.debug:
                logging.debug("Current hospitalizations: " + pformat(self.adm1_current_hosp))

        # Estimate the recent CFR during the period covered by the historical data
        cfr_delay = 15  # TODO This should come from CDC and Nij
        n_cfr = 7
        # last_cases = g_data.rolling_cum_cases[-cfr_delay-n_cfr:-cfr_delay] - g_data.rolling_cum_cases[0]
        # last_deaths = g_data.rolling_cum_deaths[-1] - g_data.rolling_cum_deaths[cfr_delay]
        last_cases = (
            g_data.rolling_cum_cases[-cfr_delay - n_cfr : -cfr_delay] - g_data.rolling_cum_cases[-cfr_delay - n_cfr - 1]
        )
        last_deaths = g_data.rolling_cum_deaths[-n_cfr:] - g_data.rolling_cum_deaths[-n_cfr - 1]
        adm1_cases = g_data.sum_adm1(last_cases.T)
        adm1_deaths = g_data.sum_adm1(last_deaths.T)
        adm1_cfr = adm1_deaths / adm1_cases
        # take harmonic mean over n days
        self.adm1_current_cfr = 1.0 / xp.mean(1.0 / adm1_cfr, axis=1)
        # from IPython import embed
        # embed()

        chr_delay = 6  # TODO This should come from I_TO_H_TIME and Nij
        n_chr = 7
        tmp = hhs_data.loc[hhs_data.date > str(self.first_date - datetime.timedelta(days=n_chr))]
        tmp = tmp.loc[tmp.date <= str(self.first_date)]
        tmp = tmp.set_index(["adm1", "date"]).sort_index()
        tmp = tmp.previous_day_admission_adult_covid_confirmed + tmp.previous_day_admission_pediatric_covid_confirmed
        cum_hosps = xp.zeros(adm1_cfr.shape)
        tmp = tmp.unstack()
        # embed()
        tmp_data = tmp.T.cumsum().to_numpy()
        tmp_ind = tmp.index.to_numpy()
        cum_hosps[tmp_ind] = tmp_data.T
        last_cases = (
            g_data.rolling_cum_cases[-chr_delay - n_chr : -chr_delay] - g_data.rolling_cum_cases[-chr_delay - n_chr - 1]
        )
        # last_hosps = cum_hosps #g_data.rolling_cum_deaths[-n_chr:] - g_data.rolling_cum_deaths[-n_chr-1]
        adm1_cases = g_data.sum_adm1(last_cases.T)
        adm1_hosps = cum_hosps  # g_data.sum_adm1(last_hosps.T)
        adm1_chr = adm1_hosps / adm1_cases
        # take harmonic mean over n days
        self.adm1_current_chr = 1.0 / xp.mean(1.0 / adm1_chr, axis=1)
        if self.debug:
            logging.debug("Current CFR: " + pformat(self.adm1_current_cfr))

        return g_data

    def reset(self, seed=None, params=None):
        """Reset the state of the model and generate new inital data from a new random seed"""
        # TODO we should refactor reset of the compartments to be real pop numbers then /Nij at the end

        # if you set a seed using the constructor, you're stuck using it forever (TODO this isn't true anymore?)
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)
            xp.random.seed(seed)

        self.iter = 0

        # reroll model params if we're doing that kind of thing
        self.g_data.Aij.perturb(self.consts.reroll_variance)
        self.params = self.bucky_params.generate_params(self.consts.reroll_variance)

        if params is not None:
            self.params = copy.deepcopy(params)

        if self.debug:
            logging.debug("params: " + pformat(self.params, width=120))

        for k in self.params:
            if type(self.params[k]).__module__ == np.__name__:
                self.params[k] = xp.asarray(self.params[k])

        # TODO consolidate all the broadcast_to calls
        self.params.H = xp.broadcast_to(self.params.H[:, None], self.Nij.shape)
        self.params.F = xp.broadcast_to(self.params.F[:, None], self.Nij.shape)

        """
        if self.use_G_ifr:  # TODO this is pretty much overwriteen with the CHR rescale...
            self.ifr[xp.isnan(self.ifr)] = 0.0
            self.params.F = self.ifr / self.params["SYM_FRAC"]
            adm0_ifr = xp.sum(self.ifr * self.Nij) / xp.sum(self.Nj)
            ifr_scale = 0.0065 / adm0_ifr  # TODO this should be in par file (its from planning scenario5)
            self.params.F = xp.clip(self.params.F * ifr_scale, 0.0, 1.0)
            self.params.F_old = self.params.F.copy()
        """

        if self.rescale_chr:
            # TODO this needs to be cleaned up BAD
            # should add a util function to do the rollups to adm1 (it shows up in case_reporting/doubling t calc too)
            # TODO this could be a population distribute type func...
            adm1_Fi = self.g_data.sum_adm1((self.params.F * self.Nij).T).T
            adm1_Ni = self.g_data.adm1_Nij  # sum_adm1(self.Nij.T)
            adm1_N = self.g_data.adm1_Nj  # sum_adm1(self.Nj)
            adm1_Fi = adm1_Fi / adm1_Ni  # TODO this will always be F, not sure what I was going for here...
            adm1_F = xp.nanmean(adm1_Fi, axis=0)

            adm1_F_fac = self.adm1_current_cfr / adm1_F
            adm0_F_fac = xp.nanmean(adm1_N * adm1_F_fac) / xp.sum(adm1_N)
            adm1_F_fac[xp.isnan(adm1_F_fac)] = adm0_F_fac

            F_RR_fac = truncnorm(1.0, self.dists.F_RR_var, size=adm1_F_fac.size, a_min=1e-6)
            adm1_F_fac = adm1_F_fac * F_RR_fac
            adm1_F_fac = xp.clip(adm1_F_fac, a_min=0.1, a_max=10.0)  # prevent extreme values
            if self.debug:
                logging.debug("adm1 cfr rescaling factor: " + pformat(adm1_F_fac))
            self.params.F = self.params.F * adm1_F_fac[self.g_data.adm1_id]

            self.params.F = xp.clip(self.params.F, a_min=1.0e-10, a_max=1.0)

            adm1_Hi = self.g_data.sum_adm1((self.params.H * self.Nij).T).T
            # adm1_Ni = self.g_data.sum_adm1(self.Nij.T)
            adm1_Hi = adm1_Hi / adm1_Ni
            adm1_H = xp.nanmean(adm1_Hi, axis=0)

            adm1_H_fac = self.adm1_current_chr / adm1_H
            adm0_H_fac = xp.nanmean(adm1_N * adm1_H_fac) / xp.sum(adm1_N)
            adm1_H_fac[xp.isnan(adm1_H_fac)] = adm0_H_fac

            H_RR_fac = truncnorm(1.0, self.dists.H_RR_var, size=adm1_H_fac.size, a_min=1e-6)
            adm1_H_fac = adm1_H_fac * H_RR_fac
            adm1_H_fac = xp.clip(adm1_H_fac, a_min=0.1, a_max=10.0)  # prevent extreme values
            if self.debug:
                logging.debug("adm1 chr rescaling factor: " + pformat(adm1_H_fac))
            self.params.H = self.params.H * adm1_H_fac[self.g_data.adm1_id]
            self.params.H = xp.clip(self.params.H, a_min=self.params.F, a_max=1.0)

        # crr_days_needed = max( #TODO this depends on all the Td params, and D_REPORT_TIME...
        case_reporting = self.estimate_reporting(
            self.g_data,
            self.params,
            cfr=self.params.F,
            days_back=22,
            min_deaths=self.consts.case_reporting_min_deaths,
        )

        self.case_reporting = approx_mPERT_sample(  # TODO these facs should go in param file
            mu=xp.clip(case_reporting, a_min=0.2, a_max=1.0),
            a=xp.clip(0.8 * case_reporting, a_min=0.2, a_max=None),
            b=xp.clip(1.2 * case_reporting, a_min=None, a_max=1.0),
            gamma=50.0,
        )

        self.doubling_t = xp.zeros(self.Nj.shape)
        # self.doubling_t = estimate_doubling_time(g_data,
        #    doubling_time_window=self.consts.doubling_t_window,
        #    mean_time_window=self.consts.doubling_t_N_historical_days,
        #    self.case_reporting,
        # )

        # if xp.any(~xp.isfinite(self.doubling_t)):
        #    logging.info("non finite doubling times, is there enough case data?")
        #    if self.debug:
        #        logging.debug(self.doubling_t)
        #        logging.debug(self.g_data.adm1_id[~xp.isfinite(self.doubling_t)])
        #    raise SimulationException

        # if self.consts.reroll_variance > 0.0:
        #    self.doubling_t *= truncnorm(1.0, self.consts.reroll_variance, size=self.doubling_t.shape, a_min=1e-6)
        #    self.doubling_t = xp.clip(self.doubling_t, 1.0, None) / 2.0

        # self.params = self.bucky_params.rescale_doubling_rate(self.doubling_t, self.params, self.g_data.Aij.diag)

        mean_case_reporting = xp.mean(self.case_reporting[-self.consts.case_reporting_N_historical_days :], axis=0)

        self.params["CASE_REPORT"] = mean_case_reporting
        self.params["THETA"] = xp.broadcast_to(
            self.params["THETA"][:, None], self.Nij.shape
        )  # TODO move all the broadcast_to's to one place, they're all over reset()
        self.params["GAMMA_H"] = xp.broadcast_to(self.params["GAMMA_H"][:, None], self.Nij.shape)
        self.params["F_eff"] = xp.clip(self.params["F"] / self.params["H"], 0.0, 1.0)

        Rt = estimate_Rt(self.g_data, self.params)
        Rt_fac = approx_mPERT_sample(**(self.dists.Rt_dist))
        Rt *= Rt_fac  # truncnorm(1.0, 1.5 * self.consts.reroll_variance, size=Rt.shape, a_min=1e-6)
        self.params["R0"] = Rt
        self.params["BETA"] = Rt * self.params["GAMMA"] / self.g_data.Aij.diag

        # init state vector (self.y)
        yy = buckyState(self.consts, self.Nij)

        if self.debug:
            logging.debug("case init")
        Ti = self.params.Ti
        current_I = xp.sum(frac_last_n_vals(self.g_data.inc_case_hist, Ti, axis=0), axis=0)

        current_I[xp.isnan(current_I)] = 0.0
        current_I[current_I < 0.0] = 0.0
        current_I *= 1.0 / (self.params["CASE_REPORT"])

        # Roll some random factors for the init compartment values
        R_fac = approx_mPERT_sample(**(self.dists.R_fac_dist))
        E_fac = approx_mPERT_sample(**(self.dists.E_fac_dist))
        H_fac = approx_mPERT_sample(**(self.dists.H_fac_dist))

        I_init = E_fac * current_I[None, :] / self.Nij / self.n_age_grps
        D_init = self.g_data.cum_death_hist[-1][None, :] / self.Nij / self.n_age_grps
        recovered_init = (
            self.g_data.cum_case_hist[-1] / self.params["SYM_FRAC"] / (self.params["CASE_REPORT"])
        ) * R_fac
        R_init = (
            (recovered_init) / self.Nij / self.n_age_grps - D_init - I_init / self.params["SYM_FRAC"]
        )  # rh handled later

        # self.params.H = self.params.H * H_fac

        # ic_frac = 1.0 / (1.0 + self.params.THETA / self.params.GAMMA_H)
        # hosp_frac = 1.0 / (1.0 + self.params.GAMMA_H / self.params.THETA)

        # print(ic_frac + hosp_frac)
        exp_frac = (
            E_fac
            * xp.ones(I_init.shape[-1])
            # * np.diag(self.A)
            # * np.sum(self.A, axis=1)
            * (self.params.R0)  # @ self.A)
            * self.params.GAMMA
            / self.params.SIGMA
        )

        ic_fac = 1.0

        yy.I = (1.0 - self.params.H) * I_init / yy.Im
        yy.Ic = ic_fac * self.params.H * I_init / yy.Im
        yy.Rh = self.params.H * I_init * self.params.GAMMA_H / self.params.THETA / yy.Rhn

        if self.rescale_chr:
            adm1_hosp = xp.zeros((self.g_data.max_adm1 + 1,), dtype=float)
            xp.scatter_add(adm1_hosp, self.g_data.adm1_id, xp.sum(yy.Rh * self.Nij, axis=(0, 1)))
            adm2_hosp_frac = (self.adm1_current_hosp / adm1_hosp)[self.g_data.adm1_id]
            adm0_hosp_frac = xp.nansum(self.adm1_current_hosp) / xp.nansum(adm1_hosp)
            adm2_hosp_frac[xp.isnan(adm2_hosp_frac)] = adm0_hosp_frac
            # self.params.F = 2. * xp.clip(1.0 / (H_fac * adm2_hosp_frac[None, :]) * self.params.F, 1.0e-10, 1.0)
            # self.params.H = xp.clip(H_fac * self.params.H * adm2_hosp_frac[None, :], self.params.F, 1.0)

            # TODO this .85 should be in param file...
            scaling_F = self.consts.F_scaling
            scaling_H = adm2_hosp_frac * H_fac
            self.params["F_eff"] = xp.clip(scaling_F * self.params["F"] / self.params["H"], 0.0, 1.0)

            # yy.I = (1.0 - self.params.H) * I_init / yy.Im
            # y[Ici] = ic_frac * self.params.H * I_init / (len(Ici))
            # y[Rhi] = hosp_frac * self.params.H * I_init / (Rhn)
            yy.Ic = scaling_H * ic_fac * self.params.H * I_init / yy.Im
            yy.Rh = (
                scaling_H
                # * self.params.CASE_REPORT
                * self.params.H
                * I_init
                * self.params.GAMMA_H
                / self.params.THETA
                / yy.Rhn
            )

        R_init -= xp.sum(yy.Rh, axis=0)

        yy.Ia = self.params.ASYM_FRAC / self.params.SYM_FRAC * I_init / yy.Im
        yy.E = exp_frac[None, :] * I_init / yy.En
        yy.R = R_init
        yy.D = D_init

        yy.init_S()
        # init the bin we're using to track incident cases (it's filled with cumulatives until we diff it later)
        yy.incC = self.g_data.cum_case_hist[-1][None, :] / self.Nij / self.n_age_grps
        self.y = yy

        # TODO assert this is 1. (need to take mean and around b/c fp err)
        # if xp.sum(self.y, axis=0)
        if xp.any(~xp.isfinite(self.y.state)):
            logging.info("nonfinite values in the state vector, something is wrong with init")
            raise SimulationException

        if self.debug:
            logging.debug("done reset()")

        # return y

    # @staticmethod need to mode the caching out b/c its in the self namespace
    def estimate_reporting(self, g_data, params, cfr, days_back=14, case_lag=None, min_deaths=100.0):
        """Estimate the case reporting rate based off observed vs. expected CFR"""

        if case_lag is None:
            adm0_cfr_by_age = xp.sum(cfr * g_data.Nij, axis=1) / xp.sum(g_data.Nj, axis=0)
            adm0_cfr_total = xp.sum(
                xp.sum(cfr * g_data.Nij, axis=1) / xp.sum(g_data.Nj, axis=0),
                axis=0,
            )
            case_lag = xp.sum(params["D_REPORT_TIME"] * adm0_cfr_by_age / adm0_cfr_total, axis=0)

        case_lag_int = int(case_lag)
        recent_cum_cases = g_data.rolling_cum_cases - g_data.rolling_cum_cases[0]
        recent_cum_deaths = g_data.rolling_cum_deaths - g_data.rolling_cum_deaths[0]
        case_lag_frac = case_lag % 1  # TODO replace with util function for the indexing
        cases_lagged = frac_last_n_vals(recent_cum_cases, days_back + case_lag_frac, offset=case_lag_int)
        if case_lag_frac:
            cases_lagged = cases_lagged[0] + cases_lagged[1:]

        # adm0
        adm0_cfr_param = xp.sum(xp.sum(cfr * g_data.Nij, axis=1) / xp.sum(g_data.Nj, axis=0), axis=0)
        if self.adm0_cfr_reported is None:
            self.adm0_cfr_reported = xp.sum(recent_cum_deaths[-days_back:], axis=1) / xp.sum(cases_lagged, axis=1)
        adm0_case_report = adm0_cfr_param / self.adm0_cfr_reported

        """
        if self.debug:
            logging.debug("Adm0 case reporting rate: " + pformat(adm0_case_report))
        if xp.any(~xp.isfinite(adm0_case_report)):
            if self.debug:
                logging.debug("adm0 case report not finite")
                logging.debug(adm0_cfr_param)
                logging.debug(self.adm0_cfr_reported)
            raise SimulationException
        """

        case_report = xp.repeat(adm0_case_report[:, None], cases_lagged.shape[-1], axis=1)

        # adm1
        adm1_cfr_param = xp.zeros((g_data.max_adm1 + 1,), dtype=float)
        adm1_totpop = g_data.adm1_Nj  # xp.zeros((self.g_data.max_adm1 + 1,), dtype=float)

        tmp_adm1_cfr = xp.sum(cfr * g_data.Nij, axis=0)

        xp.scatter_add(adm1_cfr_param, g_data.adm1_id, tmp_adm1_cfr)
        # xp.scatter_add(adm1_totpop, self.g_data.adm1_id, self.Nj)
        adm1_cfr_param /= adm1_totpop

        # adm1_cfr_reported is const, only calc it once and cache it
        if self.adm1_cfr_reported is None:
            self.adm1_deaths_reported = xp.zeros((g_data.max_adm1 + 1, days_back), dtype=float)
            adm1_lagged_cases = xp.zeros((g_data.max_adm1 + 1, days_back), dtype=float)

            xp.scatter_add(
                self.adm1_deaths_reported,
                g_data.adm1_id,
                recent_cum_deaths[-days_back:].T,
            )
            xp.scatter_add(adm1_lagged_cases, g_data.adm1_id, cases_lagged.T)

            self.adm1_cfr_reported = self.adm1_deaths_reported / adm1_lagged_cases

        adm1_case_report = (adm1_cfr_param[:, None] / self.adm1_cfr_reported)[g_data.adm1_id].T

        valid_mask = (self.adm1_deaths_reported > min_deaths)[g_data.adm1_id].T & xp.isfinite(adm1_case_report)
        case_report[valid_mask] = adm1_case_report[valid_mask]

        # adm2
        adm2_cfr_param = xp.sum(cfr * (g_data.Nij / g_data.Nj), axis=0)

        if self.adm2_cfr_reported is None:
            self.adm2_cfr_reported = recent_cum_deaths[-days_back:] / cases_lagged
        adm2_case_report = adm2_cfr_param / self.adm2_cfr_reported

        valid_adm2_cr = xp.isfinite(adm2_case_report) & (recent_cum_deaths[-days_back:] > min_deaths)
        case_report[valid_adm2_cr] = adm2_case_report[valid_adm2_cr]

        return case_report

    #
    # RHS for odes - d(sstate)/dt = F(t, state, *mats, *pars)
    # NB: requires the state vector be 1d
    #

    @staticmethod
    def RHS_func(t, y_flat, Nij, contact_mats, Aij, par, npi, aij_sparse, y):
        """RHS function for the ODEs, get's called in ivp.solve_ivp"""
        # constraint on values
        lower, upper = (0.0, 1.0)  # bounds for state vars

        # grab index of OOB values so we can zero derivatives (stability...)
        too_low = y_flat <= lower
        too_high = y_flat >= upper

        # TODO we're passing in y.state just to overwrite it, we probably need another class
        # reshape to the usual state tensor (compartment, age, node)
        y.state = y_flat.reshape(y.state_shape)

        # Clip state to be in bounds (except allocs b/c thats a counter)
        xp.clip(y.state, a_min=lower, a_max=upper, out=y.state)

        # init d(state)/dt
        dy = y.zeros_like()

        # effective params after damping w/ allocated stuff
        if npi["npi_active"]:
            # We want to avoid doing this int cast if we arent using npis b/c it forces a sync
            t_index = min(int(t), npi["r0_reduct"].shape[0] - 1)  # prevent OOB error when integrator overshoots
            BETA_eff = npi["r0_reduct"][t_index] * par["BETA"]
        else:
            BETA_eff = par["BETA"]

        F_eff = par["F_eff"]
        HOSP = par["H"]
        THETA = y.Rhn * par["THETA"]
        GAMMA = y.Im * par["GAMMA"]
        GAMMA_H = y.Im * par["GAMMA_H"]
        SIGMA = y.En * par["SIGMA"]
        SYM_FRAC = par["SYM_FRAC"]
        CASE_REPORT = par["CASE_REPORT"]

        if npi["npi_active"]:
            Cij = npi["contact_weights"][t_index] * contact_mats
            # TODO this should be c + c.T / 2
            Cij = xp.sum(Cij, axis=1)
            Cij /= xp.sum(Cij, axis=2, keepdims=True)
        else:
            Cij = contact_mats

        if npi["npi_active"]:
            if aij_sparse:
                Aij_eff = Aij.multiply(npi["mobility_reduct"][t_index])
            else:
                Aij_eff = npi["mobility_reduct"][t_index] * Aij

        else:
            Aij_eff = Aij

        # perturb Aij
        # new_R0_fracij = truncnorm(xp, 1.0, .1, size=Aij.shape, a_min=1e-6)
        # new_R0_fracij = xp.clip(new_R0_fracij, 1e-6, None)
        # A = Aij * new_R0_fracij
        # Aij_eff = A / xp.sum(A, axis=0)

        # Infectivity matrix (I made this name up, idk what its really called)
        I_tot = xp.sum(Nij * y.Itot, axis=0) - (1.0 - par["rel_inf_asym"]) * xp.sum(Nij * y.Ia, axis=0)

        # I_tmp = (Aij.T @ I_tot.T).T
        if aij_sparse:
            I_tmp = (Aij_eff.T * I_tot.T).T
        else:
            I_tmp = I_tot @ Aij  # using identity (A@B).T = B.T @ A.T

        # beta_mat = y.S * xp.squeeze((Cij @ I_tmp.T[..., None]), axis=-1).T
        beta_mat = y.S * (Cij @ xp.atleast_3d(I_tmp.T)).T[0]
        beta_mat /= Nij

        # dS/dt
        dy.S = -BETA_eff * (beta_mat)
        # dE/dt
        dy.E[0] = BETA_eff * (beta_mat) - SIGMA * y.E[0]
        dy.E[1:] = SIGMA * (y.E[:-1] - y.E[1:])

        # dI/dt
        dy.Ia[0] = (1.0 - SYM_FRAC) * SIGMA * y.E[-1] - GAMMA * y.Ia[0]
        dy.Ia[1:] = GAMMA * (y.Ia[:-1] - y.Ia[1:])

        # dIa/dt
        dy.I[0] = SYM_FRAC * (1.0 - HOSP) * SIGMA * y.E[-1] - GAMMA * y.I[0]
        dy.I[1:] = GAMMA * (y.I[:-1] - y.I[1:])

        # dIc/dt
        dy.Ic[0] = SYM_FRAC * HOSP * SIGMA * y.E[-1] - GAMMA_H * y.Ic[0]
        dy.Ic[1:] = GAMMA_H * (y.Ic[:-1] - y.Ic[1:])

        # dRhi/dt
        dy.Rh[0] = GAMMA_H * y.Ic[-1] - THETA * y.Rh[0]
        dy.Rh[1:] = THETA * (y.Rh[:-1] - y.Rh[1:])

        # dR/dt
        dy.R = GAMMA * (y.I[-1] + y.Ia[-1]) + (1.0 - F_eff) * THETA * y.Rh[-1]

        # dD/dt
        dy.D = F_eff * THETA * y.Rh[-1]

        dy.incH = GAMMA_H * y.Ic[-1]  # SYM_FRAC * HOSP * SIGMA * y.E[-1]
        dy.incC = SYM_FRAC * CASE_REPORT * SIGMA * y.E[-1]

        # bring back to 1d for the ODE api
        dy_flat = dy.state.ravel()

        # zero derivatives for things we had to clip if they are going further out of bounds
        dy_flat = xp.where(too_low & (dy_flat < 0.0), 0.0, dy_flat)
        dy_flat = xp.where(too_high & (dy_flat > 0.0), 0.0, dy_flat)

        return dy_flat

    def run_once(self, seed=None):
        """Perform one complete run of the simulation"""
        # rename to integrate or something? it also resets...

        # reset everything
        logging.debug("Resetting state")
        self.reset(seed=seed)
        logging.debug("Done reset")

        # do integration
        logging.debug("Starting integration")
        t_eval = xp.arange(0, self.t_max + self.dt, self.dt)
        sol = xp_ivp.solve_ivp(
            self.RHS_func,
            method="RK23",
            t_span=(0.0, self.t_max),
            y0=self.y.state.ravel(),
            t_eval=t_eval,
            args=(self.Nij, self.Cij, self.g_data.Aij.A, self.params, self.npi_params, self.g_data.Aij.sparse, self.y),
        )
        logging.debug("Done integration")

        return sol

    def run_multiple(self, n_mc, base_seed=42, out_columns=None):
        """Perform multiple monte carlos and return their postprocessed results"""
        seed_seq = np.random.SeedSequence(base_seed)
        success = 0
        ret = []
        pbar = tqdm.tqdm(total=n_mc, desc="Performing Monte Carlos", dynamic_ncols=True)
        while success < n_mc:
            mc_seed = seed_seq.spawn(1)[0].generate_state(1)[0]  # inc spawn key then grab next seed
            pbar.set_postfix_str(
                "seed=" + str(mc_seed),
                refresh=True,
            )
            try:
                with xp.optimize_kernels():
                    sol = self.run_once(seed=mc_seed)
                    df_data = self.postprocess_run(sol, mc_seed, out_columns)
                ret.append(df_data)
                success += 1
                pbar.update(1)
            except SimulationException:
                pass

        pbar.close()
        return ret

    # TODO Move this to a class thats like run_parser or something (that caches all the info it needs like Nij, and manages the write thread/queue)
    # Also give it methods like to_dlpack, to_pytorch, etc
    def to_feather(self, sol, base_filename, seed, output_queue):
        """Postprocess and write to disk the output of run_once"""

        df_data = self.postprocess_run(sol, seed)

        # flatten the shape
        for c in df_data:
            df_data[c] = df_data[c].ravel()

        # push the data off to the write thread
        output_queue.put((base_filename, df_data))
        # TODO we should output the per monte carlo param rolls, this got lost when we switched from hdf5

    def postprocess_run(self, sol, seed, columns=None):
        """Process the output of a run (sol, returned by the integrator) into the requested output vars"""
        if columns is None:
            columns = [
                "adm2_id",
                "date",
                "rid",
                "total_population",
                "current_hospitalizations",
                "active_asymptomatic_cases",
                "cumulative_deaths",
                "daily_hospitalizations",
                "daily_cases",
                "daily_reported_cases",
                "daily_deaths",
                "cumulative_cases",
                "cumulative_reported_cases",
                "current_icu_usage",
                "current_vent_usage",
                "case_reporting_rate",
                "R_eff",
                "doubling_t",
            ]

            columns = set(columns)

        df_data = {}

        out = buckyState(self.consts, self.Nij)

        y = sol.y.reshape(self.y.state_shape + (sol.y.shape[-1],))

        # rescale by population
        out.state = self.Nij[None, ..., None] * y

        # collapse age groups
        out.state = xp.sum(out.state, axis=1)

        # population_conserved = (xp.diff(xp.around(xp.sum(out.N, axis=(0, 1)), 1)) == 0.0).all()
        # if not population_conserved:
        #    pass  # TODO we're getting small fp errors here
        #    # print(xp.sum(xp.diff(xp.around(xp.sum(out[:incH], axis=(0, 1)), 1))))
        #    # logging.error("Population not conserved!")
        #    # print(xp.sum(xp.sum(y[:incH],axis=0)-1.))
        #    # raise SimulationException

        if "adm2_id" in columns:
            adm2_ids = np.broadcast_to(self.g_data.adm2_id[:, None], out.state.shape[1:])
            df_data["adm2_id"] = adm2_ids

        if "date" in columns:
            if self.output_dates is None:
                t_output = xp.to_cpu(sol.t)
                dates = [pd.Timestamp(self.first_date + datetime.timedelta(days=np.round(t))) for t in t_output]
                self.output_dates = np.broadcast_to(dates, out.state.shape[1:])

            dates = self.output_dates
            df_data["date"] = dates

        if "rid" in columns:
            df_data["rid"] = np.broadcast_to(seed, out.state.shape[1:])

        if "current_icu_usage" in columns or "current_vent_usage" in columns:
            icu = self.Nij[..., None] * self.params["ICU_FRAC"][:, None, None] * xp.sum(y[out.indices["Rh"]], axis=0)
            if "current_icu_usage" in columns:
                df_data["current_icu_usage"] = xp.sum(icu, axis=0)

            if "current_vent_usage" in columns:
                vent = self.params.ICU_VENT_FRAC[:, None, None] * icu
                df_data["current_vent_usage"] = xp.sum(vent, axis=0)

        if "daily_deaths" in columns:
            # prepend the min cumulative cases over the last 2 days in case in the decreased
            prepend_deaths = xp.minimum(self.g_data.cum_death_hist[-2], self.g_data.cum_death_hist[-1])
            daily_deaths = xp.diff(out.D, prepend=prepend_deaths[:, None], axis=-1)
            df_data["daily_deaths"] = daily_deaths

            if self.reject_runs:
                init_inc_death_mean = xp.mean(xp.sum(daily_deaths[:, 1:4], axis=0))
                hist_inc_death_mean = xp.mean(xp.sum(self.g_data.inc_death_hist[-7:], axis=-1))

                inc_death_rejection_fac = 2.0  # TODO These should come from the cli arg -r
                if (init_inc_death_mean > inc_death_rejection_fac * hist_inc_death_mean) or (
                    inc_death_rejection_fac * init_inc_death_mean < hist_inc_death_mean
                ):
                    logging.info("Inconsistent inc deaths, rejecting run")
                    raise SimulationException

        if "daily_cases" in columns or "daily_reported_cases" in columns:
            # prepend the min cumulative cases over the last 2 days in case in the decreased
            prepend_cases = xp.minimum(self.g_data.cum_case_hist[-2], self.g_data.cum_case_hist[-1])
            daily_reported_cases = xp.diff(out.incC, axis=-1, prepend=prepend_cases[:, None])

            if self.reject_runs:
                init_inc_case_mean = xp.mean(xp.sum(daily_reported_cases[:, 1:4], axis=0))
                hist_inc_case_mean = xp.mean(xp.sum(self.g_data.inc_case_hist[-7:], axis=-1))

                inc_case_rejection_fac = 1.5  # TODO These should come from the cli arg -r
                if (init_inc_case_mean > inc_case_rejection_fac * hist_inc_case_mean) or (
                    inc_case_rejection_fac * init_inc_case_mean < hist_inc_case_mean
                ):
                    logging.info("Inconsistent inc cases, rejecting run")
                    raise SimulationException

            if "daily_reported_cases" in columns:
                df_data["daily_reported_cases"] = daily_reported_cases

            if "daily_cases" in columns:
                daily_cases_total = daily_reported_cases / self.params.CASE_REPORT[:, None]
                df_data["daily_cases"] = daily_cases_total

        if "cumulative_reported_cases" in columns:
            cum_cases_reported = out.incC
            df_data["cumulative_reported_cases"] = cum_cases_reported

        if "cumulative_cases" in columns:
            cum_cases_total = out.incC / self.params.CASE_REPORT[:, None]
            df_data["cumulative_cases"] = cum_cases_total

        if "daily_hospitalizations" in columns:
            out.incH[:, 0] = out.incH[:, 1]
            daily_hosp = xp.diff(out.incH, axis=-1, prepend=out.incH[:, 0][..., None])
            df_data["daily_hospitalizations"] = daily_hosp

        if "total_population" in columns:
            N = xp.broadcast_to(self.Nj[..., None], out.state.shape[1:])
            df_data["total_population"] = N

        if "current_hospitalizations" in columns:
            hosps = xp.sum(out.Rh, axis=0)  # why not just using .H?
            df_data["current_hospitalizations"] = hosps

        if "cumulative_deaths" in columns:
            cum_deaths = out.D
            df_data["cumulative_deaths"] = cum_deaths

        if "active_asymptomatic_cases" in columns:
            asym_I = xp.sum(out.Ia, axis=0)
            df_data["active_asymptomatic_cases"] = asym_I

        if "case_reporting_rate" in columns:
            crr = xp.broadcast_to(self.params.CASE_REPORT[:, None], adm2_ids.shape)
            df_data["case_reporting_rate"] = crr

        if "R_eff" in columns:
            r_eff = self.npi_params["r0_reduct"].T * np.broadcast_to(
                (self.params.R0 * self.g_data.Aij.diag)[:, None], adm2_ids.shape
            )
            df_data["R_eff"] = r_eff

        if "doubling_t" in columns:
            Td = np.broadcast_to(self.doubling_t[:, None], adm2_ids.shape)
            df_data["doubling_t"] = Td

        # Collapse the gamma-distributed compartments and move everything to cpu
        negative_values = False
        for k in df_data:
            # if df_data[k].ndim == 2:
            #    df_data[k] = xp.sum(df_data[k], axis=0)

            if k != "date" and xp.any(xp.around(df_data[k], 2) < 0.0):
                logging.info("Negative values present in " + k)
                negative_values = True

        if negative_values and self.reject_runs:
            logging.info("Rejecting run b/c of negative values in output")
            raise SimulationException

        return df_data

        if output:
            os.makedirs(output_folder, exist_ok=True)
            output_queue.put((os.path.join(output_folder, str(seed)), df_data))
        # TODO we should output the per monte carlo param rolls, this got lost when we switched from hdf5


def main(args=None):
    """Main method for a complete simulation called with a set of CLI args"""
    if args is None:
        args = sys.argv[1:]
    args = parser.parse_args(args=args)

    if args.gpu:
        logging.info("Using GPU backend")
        use_cupy(optimize=args.opt)

    reimport_numerical_libs("model.main.main")

    warnings.simplefilter(action="ignore", category=xp.ExperimentalWarning)

    if not os.path.exists(args.output_dir):
        os.mkdir(args.output_dir)

    loglevel = 30 - 10 * min(args.verbosity, 2)
    runid = get_runid()

    # Setup output folder TODO change over to pathlib
    output_folder = os.path.join(args.output_dir, runid)
    if not os.path.exists(output_folder):
        os.mkdir(output_folder)
    fh = logging.FileHandler(output_folder + "/stdout")
    fh.setLevel(logging.DEBUG)
    logging.basicConfig(
        level=loglevel,
        format="%(asctime)s - %(levelname)s - %(filename)s:%(funcName)s:%(lineno)d - %(message)s",
        handlers=[TqdmLoggingHandler()],
    )
    debug_mode = loglevel < 20

    # TODO we should output the logs to output_dir too...
    _banner()

    # TODO move the write_thread stuff to a util (postprocess uses something similar)
    to_write = queue.Queue(maxsize=100)

    def writer():
        """Write thread loop that pulls from an async queue"""
        # Call to_write.get() until it returns None
        stream = xp.cuda.Stream(non_blocking=True) if args.gpu else None
        for base_fname, df_data in iter(to_write.get, None):
            cpu_data = {k: xp.to_cpu(v, stream=stream) for k, v in df_data.items()}
            if stream is not None:
                stream.synchronize()
            df = pd.DataFrame(cpu_data)
            for date, date_df in df.groupby("date", as_index=False):
                fname = base_fname + "_" + str(date.date()) + ".feather"
                date_df.reset_index().to_feather(fname)

    write_thread = threading.Thread(target=writer, daemon=True)
    write_thread.start()

    logging.info(f"command line args: {args}")
    env = buckyModelCovid(
        debug=debug_mode,
        sparse_aij=(not args.dense),
        t_max=args.days,
        graph_file=args.graph_file,
        par_file=args.par_file,
        npi_file=args.npi_file,
        disable_npi=args.disable_npi,
        reject_runs=args.reject_runs,
    )

    seed_seq = np.random.SeedSequence(args.seed)

    total_start = datetime.datetime.now()
    success = 0
    n_runs = 0
    pbar = tqdm.tqdm(total=args.n_mc, desc="Performing Monte Carlos", dynamic_ncols=True)
    try:
        while success < args.n_mc:
            mc_seed = seed_seq.spawn(1)[0].generate_state(1)[0]  # inc spawn key then grab next seed
            pbar.set_postfix_str(
                "seed="
                + str(mc_seed)
                + ", rej%="  # TODO disable rej% if not -r
                + str(np.around(float(n_runs - success) / (n_runs + 0.00001) * 100, 1)),
                refresh=True,
            )
            try:
                n_runs += 1
                with xp.optimize_kernels():
                    sol = env.run_once(seed=mc_seed)
                    base_filename = os.path.join(output_folder, str(mc_seed))
                    env.to_feather(sol, base_filename, mc_seed, output_queue=to_write)

                success += 1
                pbar.update(1)
            except SimulationException:
                pass

    except (KeyboardInterrupt, SystemExit):
        logging.warning("Caught SIGINT, cleaning up")
        to_write.put(None)
        write_thread.join()
    finally:
        to_write.put(None)
        write_thread.join()
        pbar.close()
        logging.info(f"Total runtime: {datetime.datetime.now() - total_start}")


if __name__ == "__main__":
    main()
