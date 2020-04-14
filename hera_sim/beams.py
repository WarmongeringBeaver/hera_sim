import numpy as np
from pyuvsim import AnalyticBeam
from numpy.polynomial.chebyshev import chebval, chebfit
from scipy.optimize import curve_fit


class PolyBeam(AnalyticBeam):
    
    def __init__(self, beam_coeffs=[], spectral_index=0.0, ref_freq=1e8):
        """
        Analytic, azimuthally-symmetric beam model based on Chebyshev 
        polynomials. Defines an object with similar functionality to 
        pyuvdata.UVBeam.

        Parameters
        ----------
        beam_coeffs: array_like
            Co-efficients of the Chebyshev polynomial.

        spectral_index : float, optional
            Spectral index of the frequency-dependent power law scaling to 
            apply to the width of the beam. Default: 0.0.

        ref_freq : float, optional
            Reference frequency for the beam width scaling power law, in Hz. 
            Default: 1e8.
        """
        self.ref_freq = ref_freq
        self.spectral_index = spectral_index
        self.data_normalization = 'peak'
        self.freq_interp_kind = None
        self.beam_type = 'efield'
        self.beam_coeffs = beam_coeffs
    
    
    def peak_normalize(self):
        # Not required
        pass
        
        
    def interp(self, az_array, za_array, freq_array, reuse_spline=None):
        """
        Evaluate the primary beam at given az, za locations (in radians).

        Parameters
        ----------
        az_array : array_like
            Azimuth values in radians (same length as za_array). The azimuth 
            here has the UVBeam convention: North of East(East=0, North=pi/2)
        
        za_array : array_like
            Zenith angle values in radians (same length as az_array).
        
        freq_array : array_like
            Frequency values to evaluate at
        
        reuse_spline : bool, optional
            Does nothing for analytic beams. Here for compatibility with UVBeam.

        Returns
        -------
        interp_data : array_like
            Array of beam values, shape (Naxes_vec, Nspws, Nfeeds or Npols,
            Nfreqs or freq_array.size if freq_array is passed,
            Npixels/(Naxis1, Naxis2) or az_array.size if az/za_arrays are passed)
        
        interp_basis_vector : array_like
            Array of interpolated basis vectors (or self.basis_vector_array
            if az/za_arrays are not passed), shape: (Naxes_vec, Ncomponents_vec,
            Npixels/(Naxis1, Naxis2) or az_array.size if az/za_arrays are passed)
        """
        # Empty data array
        interp_data = np.zeros((2, 1, 2, freq_array.size, az_array.size),
                               dtype=np.float)
        
        # Frequency scaling
        fscale = (freq_array / self.ref_freq)**self.spectral_index
        
        # Transformed zenith angle, also scaled with frequency
        x = 2.*np.sin(za_array[np.newaxis, ...] / fscale[:, np.newaxis]) - 1.
        
        # Primary beam values from Chebyshev polynomial
        values = chebval(x, self.beam_coeffs)
        central_val = chebval(-1., self.beam_coeffs)
        values /= central_val # ensure normalized to 1 at za=0
        
        # Set values
        interp_data[1, 0, 0, :, :] = values
        interp_data[0, 0, 1, :, :] = values
        interp_basis_vector = None
    
        #FIXME: Check if power beam is being handled correctly
        if self.beam_type == 'power':
            # Cross-multiplying feeds, adding vector components
            pairs = [(i, j) for i in range(2) for j in range(2)]
            power_data = np.zeros((1, 1, 4) + values.shape, dtype=np.float)
            for pol_i, pair in enumerate(pairs):
                power_data[:, :, pol_i] = ((interp_data[0, :, pair[0]]
                                           * np.conj(interp_data[0, :, pair[1]]))
                                           + (interp_data[1, :, pair[0]]
                                           * np.conj(interp_data[1, :, pair[1]])))
            interp_data = power_data

        return interp_data, interp_basis_vector

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        if self.beam_coeffs == other.beam_coeffs:
            return True
        else:
            return False


class PerturbedPolyBeam(PolyBeam):
    
    def __init__(self, perturb_coeffs=None, perturb_scale=0.1, 
                 mainlobe_width=None, mainlobe_scale=1., transition_width=0.05, 
                 **kwargs):
        """
        Analytic, azimuthally-symmetric beam model based on Chebyshev 
        polynomials, with perturbations added to the mainlobe and/or sidelobes.
        Defines an object with similar functionality to pyuvdata.UVBeam.
        
        The perturbations are implemented in two different ways, depending on 
        whether the main lobe or sidelobes are being perturbed.
        
        Mainlobe: A Gaussian of width FWHM is subtracted and then a new 
        Gaussian with width `mainlobe_width` is added back in. This perturbs 
        the width of the primary beam mainlobe, but leaves the sidelobes mostly 
        unchanged.
        
        Sidelobes: The baseline primary beam model, PB, is moduled by a (sine)
        Fourier series at angles above some only 
        
        Parameters
        ----------
        perturb_coeffs : array_like, optional
            Array of floats with the coefficients of a (sine-only) Fourier 
            series that will be used to modulate the base Chebyshev primary 
            beam model. Default: None.
        
        perturb_scale : float, optional
            Overall scale of the primary beam modulation. Must be less than 1, 
            otherwise the primary beam can go negative. Default: 0.1.
        
        mainlobe_width : float
            Width of the mainlobe, in radians. This determines the width of the 
            Gaussian mainlobe model that is subtracted, as well as the location 
            of the transition between the mainlobe and sidelobe regimes.
        
        mainlobe_scale : float, optional
            Factor to apply to the FHWM of the Gaussian that is used to rescale 
            the mainlobe. Default: 1.
        
        transition_width : float, optional
            Width of the smooth transition between the range of angles 
            considered to be in the mainlobe vs in the sidelobes, in radians. 
            Default: 0.05.
        
        beam_coeffs: array_like
            Co-efficients of the baseline Chebyshev polynomial.

        spectral_index : float, optional
            Spectral index of the frequency-dependent power law scaling to 
            apply to the width of the beam. Default: 0.0.

        ref_freq : float, optional
            Reference frequency for the beam width scaling power law, in Hz. 
            Default: None.
        """
        # Initialize base class
        super(PerturbedPolyBeam, self).__init__(**kwargs)
        
        # Check for valid input parameters
        if mainlobe_width is None:
            raise ValueError("Must specify a value for 'mainlobe_width' kwarg")
        
        # Set parameters
        self.perturb_coeffs = perturb_coeffs
        if self.perturb_coeffs is not None:
            self.nmodes = self.perturb_coeffs.size
        else:
            self.nmodes = 0
        self.perturb_scale = perturb_scale
        self.mainlobe_width = mainlobe_width
        self.mainlobe_scale = mainlobe_scale
        self.transition_width = transition_width
        
        # Sanity checks
        if perturb_scale >= 1.:
            raise ValueError("'perturb_scale' must be less than 1; otherwise "
                             "the beam can go negative.")
    
    
    def interp(self, *args, **kwargs):
        # FIXME: This should include a frequency scaling of the zenith angle
        
        # Get positional arguments
        az_array, za_array, freq_array, = (arg for arg in args)
        
        # Call interp() method on parent class
        interp_fn = super(PerturbedPolyBeam, self).interp
        interp_data, interp_basis_vector = interp_fn(*args, **kwargs)
        
        # Smooth step function
        step = 0.5 * (1. + np.tanh((za_array - self.mainlobe_width)
                                   / self.transition_width))
        
        # Add sidelobe perturbations
        if self.nmodes != 0:
            # Build Fourier series
            f_fac = 2.*np.pi / (np.pi/2.) #  Fourier series with period pi/2
            sine_modes = np.array([np.sin(f_fac * n * za_array) 
                                   for n in range(self.nmodes)])
            
            # Construct Fourier series perturbation
            p = np.sum(self.perturb_coeffs[:,np.newaxis] * sine_modes, axis=0)
            p /= (np.max(p) - np.min(p)) / 2.
            
            # Modulate primary beam by perturbation function
            interp_data *= (1. + step * p * self.perturb_scale)
        
        # Add mainlobe stretch factor
        if self.mainlobe_scale != 1.:
            # Subtract and re-add Gaussian normalized to 1 at za = 0
            w = self.mainlobe_width / 2.
            mainlobe0 = np.exp(-0.5*(za_array / w)**2.)
            mainlobe_pert = np.exp(-0.5*(za_array/(w * self.mainlobe_scale))**2.)
            interp_data += (1. - step) * (mainlobe_pert - mainlobe0)
        
        return interp_data, interp_basis_vector
        
