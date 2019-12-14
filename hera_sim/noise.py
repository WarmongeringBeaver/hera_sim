"""Make some noise."""

import os
import astropy.constants as const
import astropy.units as u
import numpy as np

from .components import registry
from .data import DATA_PATH
from .interpolators import Tsky
from . import utils

# to minimize breaking changes
HERA_Tsky_mdl = {
    pol : Tsky(os.path.join(DATA_PATH, "HERA_Tsky_Reformatted.npz"), pol=pol)
    for pol in ("xx", "yy")
}

# XXX re-add resample_Tsky function but add a DeprecationWarning

@registry
class Noise:
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

class ThermalNoise(Noise):
    __aliases__ = ("thermal_noise", )

    def __init__(self, Tsky_mdl=None, omega_p=None, 
                 integration_time=None, channel_width=None,
                 Trx=0):
        # TODO: docstring
        """
        """
        super().__init__(
            Tsky_mdl=Tsky_mdl,
            omega_p=omega_p,
            integration_time=integration_time,
            channel_width=channel_width,
            Trx=Trx
        )

    def __call__(self, lsts, freqs, **kwargs):
        # TODO: docstring
        """
        """
        # validate the kwargs
        self._check_kwargs(**kwargs)

        # unpack the kwargs
        (Tsky_mdl, omega_p, integration_time, channel_width, 
            Trx) = self._extract_kwarg_values(**kwargs)

        # make sure a sky temperature model is specified
        if Tsky_mdl is None:
            raise ValueError(
                "A sky temperature model is required "
                "to use this function."
            )

        # get the channel width in Hz if not specified
        if channel_width is None:
            channel_width = np.mean(np.diff(freqs)) * 1e9
        
        # get the integration time if not specified
        if integration_time is None:
            integration_time = np.mean(np.diff(lsts)) / (2*np.pi)
            integration_time *= u.sday.to("s")
        
        # default to H1C beam if not specified
        if omega_p is None:
            omega_p = np.load(os.path.join(DATA_PATH,
                                           "HERA_H1C_BEAM_POLY.npy"))
            omega_p = np.polyval(omega_p, freqs)
        
        # support passing beam as an interpolator
        if callable(omega_p):
            omega_p = omega_p(freqs)

        # resample the sky temperature model and add the receiver temp
        Tsky = Tsky_mdl(lsts, freqs) + Trx
    
        # calculate noise visibility in units of K, assuming Tsky
        # is in units of K
        vis = Tsky / np.sqrt(integration_time * channel_width)
        
        # convert vis to Jy
        # XXX why the reshape?
        vis /= utils.Jy2T(freqs, omega_p).reshape(1, -1)
        
        # make it noisy
        return utils.gen_white_noise(size=vis.shape) * vis

thermal_noise = ThermalNoise()

sky_noise_jy = \
    lambda lsts, freqs, **kwargs : thermal_noise(lsts, freqs, Trx=0, **kwargs)
