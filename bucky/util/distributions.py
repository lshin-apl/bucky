"""Provide probability distributions used by the model that aren't in numpy/cupy."""

import numpy as np
import scipy.special as sc

from ..numerical_libs import reimport_numerical_libs, xp


def kumaraswamy_invcdf(a, b, u):
    """Inverse CDF of the Kumaraswamy distribution"""
    return (1.0 - (1.0 - u) ** (1.0 / b)) ** (1.0 / a)


def approx_betaincinv(alp1, alp2, u):
    """Approximate betaincinv using Kumaraswamy after converting the params so that the means and modes are equal"""
    a = alp1
    b = ((alp1 - 1.0) ** (1.0 - alp1) * (alp1 + alp2 - 2.0) ** alp1 + 1) / alp1
    return kumaraswamy_invcdf(a, xp.real(b), u)


def approx_mPERT_sample(mu, a=0.0, b=1.0, gamma=4.0, var=None):
    """Approximate sample from an mPERT distribution that uses a Kumaraswamy distribution in place of the incomplete beta; Supports Cupy."""
    reimport_numerical_libs("util.distributions.approx_mPERT_sample")
    mu, a, b = xp.atleast_1d(mu, a, b)
    alp1 = 1.0 + gamma * ((mu - a) / (b - a))
    alp2 = 1.0 + gamma * ((b - mu) / (b - a))
    u = xp.random.random_sample(mu.shape)
    alp3 = approx_betaincinv(alp1, alp2, u)
    return (b - a) * alp3 + a


# TODO only works on cpu atm
# we'd need to implement betaincinv ourselves in cupy
def mPERT_sample(mu, a=0.0, b=1.0, gamma=4.0, var=None):
    """Provide a vectorized Modified PERT distribution.

    Parameters
    ----------
    mu : float or ndarray
        Mean value for the PERT distribution.
    a : float or ndarray
        Lower bound for the distribution.
    b : float or ndarray
        Upper bound for the distribution.
    gamma : float or ndarray
        Shape paramter.
    var : float, ndarray or None
        Variance of the distribution. If var != None,
        gamma will be calcuated to meet the desired variance.

    Returns
    -------
    out : float or ndarray
        Samples drawn from the specified mPERT distribution.
        Shape is the broadcasted shape of the the input parameters.

    """
    mu, a, b = np.atleast_1d(mu, a, b)
    if var is not None:
        gamma = (mu - a) * (b - mu) / var - 3.0
    alp1 = 1.0 + gamma * ((mu - a) / (b - a))
    alp2 = 1.0 + gamma * ((b - mu) / (b - a))
    u = np.random.random_sample(mu.shape)
    alp3 = sc.betaincinv(alp1, alp2, u)
    return (b - a) * alp3 + a


def truncnorm(loc=0.0, scale=1.0, size=1, a_min=None, a_max=None):
    """Provide a vectorized truncnorm implementation that is compatible with cupy.

    The output is calculated by using the numpy/cupy random.normal() and
    truncted via rejection sampling. The interface is intended to mirror
    the scipy implementation of truncnorm.

    Parameters
    ----------


    Returns
    -------

    """
    reimport_numerical_libs("util.distributions.truncnorm")

    ret = xp.random.normal(loc, scale, size)
    if a_min is None:
        a_min = xp.array(-xp.inf)
    if a_max is None:
        a_max = xp.array(xp.inf)

    while True:
        valid = (ret > a_min) & (ret < a_max)
        if valid.all():
            return ret
        ret[~valid] = xp.random.normal(loc, scale, ret[~valid].shape)
