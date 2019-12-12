"""Reimagining of the foregrounds module, using an object-oriented approach."""

import numpy as np
import astropy.constants as const
import astropy.units as u
from aipy.const import sidereal_day
from abc import abstractmethod
from cached_property import cached_property

from . import utils
from .components import registry

@registry
class Foreground:
    pass

class DiffuseForeground(Foreground):
    # TODO: fill in docstring
    """
    
    """
    __aliases__ = ("diffuse_foreground", )

    def __init__(self, Tsky_mdl=None, omega_p=None,
                 delay_filter_kwargs={"standoff" : 0.0,
                                      "delay_filter_type" : "tophat"}, 
                 fringe_filter_kwargs={"fringe_filter_type" : "tophat"}):
        # TODO: fill in docstring
        """

        """
        super().__init__(
            Tsky_mdl=Tsky_mdl,
            omega_p=omega_p,
            delay_filter_kwargs=delay_filter_kwargs,
            fringe_filter_kwargs=fringe_filter_kwargs
        )

    def __call__(self, lsts, freqs, bl_vec, **kwargs):
        # TODO: fill in docstring
        """
        
        """
        # validate the kwargs
        self._check_kwargs(**kwargs)

        # unpack the kwargs
        (Tsky_mdl, omega_p, delay_filter_kwargs,
            fringe_filter_kwargs) = self._extract_kwarg_values(**kwargs)

        if Tsky_mdl is None:
            raise ValueError(
                "A sky temperature model must be specified in "
                "order to use this function."
            )

        if omega_p is None:
            raise ValueError(
                "A beam area array or interpolation object is "
                "required to use this function."
            )

        # support passing beam as an interpolator
        if callable(omega_p):
            omega_p = omega_p(freqs)

        # resample the sky temperature model
        Tsky = Tsky_mdl(lsts, freqs)
        vis = np.asarray(Tsky / utils.Jy2T(freqs, omega_p), np.complex)

        if np.isclose(np.linalg.norm(bl_vec), 0):
            return vis

        vis *= utils.gen_white_noise(vis.shape)

        vis = utils.rough_fringe_filter(vis, lsts, freqs, bl_vec[0], 
                                        **fringe_filter_kwargs)

        vis = utils.rough_delay_filter(vis, freqs, np.linalg.norm(bl_vec),
                                       **delay_filter_kwargs)

        return vis

class PointSourceForeground(Foreground):
    # TODO: fill in docstring
    """

    """
    __aliases__ = ("pntsrc_foreground",)

    def __init__(self, nsrcs=1000, Smin=0.3, Smax=300, beta=-1.5,
                 spectral_index_mean=-1, spectral_index_std=0.5, 
                 reference_freq=0.15):
        # TODO: fill in docstring
        """

        """
        super().__init__(
            nsrcs=nsrcs, 
            Smin=Smin, 
            Smax=Smax, 
            beta=beta,
            spectral_index_mean=spectral_index_mean,
            spectral_index_std=spectral_index_std,
            reference_freq=reference_freq
        )

    def __call__(self, lsts, freqs, bl_vec, **kwargs):
        # TODO: fill in docstring
        """
        
        """
        # validate the kwargs
        self._check_kwargs(**kwargs)

        # unpack the kwargs
        (nsrcs, Smin, Smax, beta, spectral_index_mean, 
            spectral_index_std, f0) = self._extract_kwarg_values(**kwargs)


        # get baseline length in nanoseconds
        bl_len_ns = np.linalg.norm(bl_vec) / const.c.value * u.s.to("ns")

        # randomly generate source RAs
        ras = np.random.uniform(0, 2 * np.pi, nsrcs)

        # draw spectral indices from normal distribution
        spec_indices = np.random.normal(spectral_index_mean,
                                        spectral_index_std,
                                        size=nsrcs)

        # calculate beam width, hardcoded for HERA
        beam_width = (40 * 60) * (f0 / freqs) / sidereal_day * 2 * np.pi

        # draw flux densities from a power law
        alpha = beta + 1

        # XXX check to make sure this actually gives a power law
        flux_densities = (Smax ** alpha + Smin ** alpha 
                         * (1 - np.random.uniform(size=nsrcs))) ** (1 / alpha)

        # initialize the visibility array
        vis = np.zeros((lsts.size, freqs.size), dtype=np.complex)

        # iterate over ra, flux, spectral indices
        for ra, flux, index in zip(ras, flux_densities, spec_indices):
            # find which lst index to use?
            lst_ind = np.argmin(np.abs(utils.compute_ha(lsts, ra)))

            # slight offset in delay? why??
            dtau = np.random.uniform(-1, 1) * 0.1 * bl_len_ns

            # fill in the corresponding region of the visibility array
            vis[lst_ind, :] += flux * (freqs / f0) ** index

            # now multiply in the phase
            vis[lst_ind, :] *= np.exp(2j * np.pi * freqs * dtau)

        # get hour angles for lsts at 0 RA (why?)
        has = utils.compute_ha(lsts, 0)
        
        # convolve vis with beam at each frequency
        for j, freq in enumerate(freqs):
            # first calculate the beam, using truncated Gaussian model
            beam = np.exp(-has ** 2 / (2 * beam_width[j] ** 2))
            beam = np.where(np.abs(has) > np.pi / 2, 0, beam)

            # who the hell knows what this does
            w = 0.9 * bl_len_ns * np.sin(has) * freq

            phase = np.exp(2j * np.pi * w)
            
            # define the convolving kernel
            kernel = beam * phase

            # now actually convolve the kernel and the raw vis
            vis[:, j] = np.fft.ifft(np.fft.fft(kernel) * np.fft.fft(vis[:, j]))

        return vis

diffuse_foreground = DiffuseForeground()
pntsrc_foreground = PointSourceForeground()
