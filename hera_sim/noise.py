"""Make some noise."""

import os
import warnings
import astropy.units as u
import numpy as np

from .components import registry
from . import DATA_PATH
from .interpolators import Tsky
from . import utils

# to minimize breaking changes
HERA_Tsky_mdl = {
    pol: Tsky(DATA_PATH / "HERA_Tsky_Reformatted.npz", pol=pol) for pol in ("xx", "yy")
}


@registry
class Noise:
    """Base class for thermal noise models."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class ThermalNoise(Noise):
    _alias = ("thermal_noise",)

    def __init__(
        self,
        Tsky_mdl=None,
        omega_p=None,
        integration_time=None,
        channel_width=None,
        Trx=0,
        autovis=None,
    ):
        """Generate thermal noise based on a sky model.

        Parameters
        ----------
        Tsky_mdl : callable, optional
            A function of ``(lsts, freq)`` that returns the integrated
            sky temperature at that time/frequency. If not provided, assumes
            a power-law temperature with 180 K at 180 MHz and spectral index
            of -2.5.
        omega_p : array_like or callable, optional
            If callable, a function of frequency giving the integrated beam
            area. If an array, same length as given frequencies.
        integration_time : float, optional
            Integration time in seconds. By default, use the average difference
            between given LSTs.
        channel_width : float, optional
            Channel width in Hz, by default the mean difference between frequencies.
        Trx : float, optional
            Receiver temperature in K
        autovis : float, optional
            Autocorrelation visibility amplitude. Used if provided instead of ``Tsky_mdl``.
        """
        super().__init__(
            Tsky_mdl=Tsky_mdl,
            omega_p=omega_p,
            integration_time=integration_time,
            channel_width=channel_width,
            Trx=Trx,
            autovis=autovis,
        )

    def __call__(self, lsts, freqs, **kwargs):
        # TODO: docstring
        """
        """
        # validate the kwargs
        self._check_kwargs(**kwargs)

        # unpack the kwargs
        (
            Tsky_mdl,
            omega_p,
            integration_time,
            channel_width,
            Trx,
            autovis,
        ) = self._extract_kwarg_values(**kwargs)

        # get the channel width in Hz if not specified
        if channel_width is None:
            channel_width = np.mean(np.diff(freqs)) * 1e9

        # get the integration time if not specified
        if integration_time is None:
            integration_time = np.mean(np.diff(lsts)) / (2 * np.pi)
            integration_time *= u.sday.to("s")

        # default to H1C beam if not specified
        # FIXME: these three lines currently not tested
        if omega_p is None:
            omega_p = np.load(DATA_PATH / "HERA_H1C_BEAM_POLY.npy")
            omega_p = np.polyval(omega_p, freqs)

        # support passing beam as an interpolator
        if callable(omega_p):
            omega_p = omega_p(freqs)

        # get the sky temperature; use an autocorrelation if provided
        if autovis is not None and not np.all(np.isclose(autovis, 0)):
            Tsky = autovis * utils.jansky_to_kelvin(freqs, omega_p).reshape(1, -1)
        else:
            Tsky = self.resample_Tsky(lsts, freqs, Tsky_mdl=Tsky_mdl)

        # add in the receiver temperature
        Tsky += Trx

        # calculate noise visibility in units of K, assuming Tsky
        # is in units of K
        vis = Tsky / np.sqrt(integration_time * channel_width)

        # convert vis to Jy; reshape to allow for multiplication.
        vis /= utils.jansky_to_kelvin(freqs, omega_p).reshape(1, -1)

        # make it noisy
        return utils.gen_white_noise(size=vis.shape) * vis

    @staticmethod
    def resample_Tsky(lsts, freqs, Tsky_mdl=None, Tsky=180.0, mfreq=0.18, index=-2.5):
        """Evaluate an array of sky temperatures.

        Parameters
        ----------
        lsts : array-like of float
            LSTs at which to sample the sky tmeperature.
        freqs : array_like of float
            The frequencies at which to sample the temperature, in GHz.
        Tsky_mdl : callable, optional
            Callable function of ``(lsts, freqs)``. If not given, use a power-law
            defined by the next three parameters.
        Tsky : float, optional
            Sky temperature at ``mfreq``. Only used if ``Tsky_mdl`` not given.
        mfreq : float, optional
            Reference frequency for sky temperature. Only used if ``Tsky_mdl`` not given.
        index : float, optional
            Spectral index of sky temperature model. Only used if ``Tsky_mdl`` not given.

        Returns
        -------
        ndarray
            The sky temperature as a 2D array, first axis LSTs and second axis freqs.
        """
        # maybe add a DeprecationWarning?

        # actually resample the sky model if it's an interpolation object
        if Tsky_mdl is not None:
            tsky = Tsky_mdl(lsts, freqs)
        else:
            # use a power law if there's no sky model
            tsky = Tsky * (freqs / mfreq) ** index
            # reshape it appropriately
            tsky = np.resize(tsky, (lsts.size, freqs.size))
        return tsky


# make the old functions discoverable
resample_Tsky = ThermalNoise.resample_Tsky
thermal_noise = ThermalNoise()


def sky_noise_jy(lsts: np.ndarray, freqs: np.ndarray, **kwargs):
    """Generate thermal noise at particular LSTs and frequencies.

    Parameters
    ----------
    lsts : array_like
        LSTs at which to compute the sky noise.
    freqs : array_like
        Frequencies at which to compute the sky noise.

    Other Parameters
    ----------------
    See :class:`ThermalNoise`.

    Returns
    -------
    ndarray
        2D array of white noise in LST/freq.
    """
    return thermal_noise(lsts, freqs, Trx=0, **kwargs)


def white_noise(*args, **kwargs):
    """Generate white noise in an array.

    Deprecated. Use ``utils.gen_white_noise`` instead.
    """
    warnings.warn("white_noise is being deprecated. Use utils.gen_white_noise instead.")
    return utils.gen_white_noise(*args, **kwargs)
