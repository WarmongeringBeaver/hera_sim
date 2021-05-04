from __future__ import division
from builtins import range
import numpy as np
from scipy.interpolate import RectBivariateSpline
import healpy
import pyuvdata

from . import conversions
from .simulators import VisibilitySimulator

from astropy.constants import c


class VisCPU(VisibilitySimulator):
    """
    vis_cpu visibility simulator.

    This is a fast, simple visibility simulator that is intended to be
    replaced by vis_gpu. It extends :class:`VisibilitySimulator`.
    """

    def __init__(self, bm_pix=100, use_pixel_beams=True, polarized=False, 
                 precision=1, use_gpu=False, mpi_comm=None, **kwargs):
        """
        Parameters
        ----------
        bm_pix : int, optional
            The number of pixels along a side in the beam map when
            converted to (l, m) coordinates. Defaults to 100.
        
        use_pixel_beams : bool, optional
            Whether to use primary beams that have been pixelated onto a 2D 
            grid, or directly evaluate the primary beams using the available 
            UVBeam objects. Default: True.
        
        polarized: bool, optional
            Whether to simulate a full polarized response. Default: False.
        
        precision : int, optional
            Which precision level to use for floats and complex numbers. 
            Allowed values:
                - 1: float32, complex64
                - 2: float64, complex128
            Default: 1.
        
        use_gpu : bool, optional
            Whether to use the GPU version of vis_cpu or not. Default: False.
        
        mpi_comm : MPI communicator
            MPI communicator, for parallelization.
        
        **kwargs
            Arguments of :class:`VisibilitySimulator`.
        """
        assert precision in (1,2)
        self._precision = precision
        if precision == 1:
            self._real_dtype = np.float32
            self._complex_dtype = np.complex64
        else:
            self._real_dtype = np.float64
            self._complex_dtype = np.complex128

        if use_gpu and mpi_comm is not None and mpi_comm.Get_size() > 1:
              raise RuntimeError("Can't use multiple MPI processes with GPU (yet)")

        if use_gpu:
            if not use_pixel_beams:
                raise RuntimeError("GPU can only be used with pixel beams (use_pixel_beams=True)") 
            try:
                from hera_gpu.vis import vis_gpu
                self._vis_cpu = vis_gpu
            except ImportError:
                raise ImportError(
                    'GPU acceleration requires hera_gpu (`pip install hera_sim[gpu]`).'
                )
        else:
            self._vis_cpu = vis_cpu
        
        self.polarized = polarized
        self.use_gpu = use_gpu 
        self.bm_pix = bm_pix
        self.use_pixel_beams = use_pixel_beams
        self.mpi_comm = mpi_comm
        
        super(VisCPU, self).__init__(validate=False, **kwargs)
          
        # If beam ids and beam lists are mis-matched, expand the beam list 
        # or raise an error
        if len(self.beams) != len(self.beam_ids):
            
            # If N_beams > 1 and N_beams != N_ants, raise an error
            if len(self.beams) > 1:
                raise ValueError("Specified %d beams for %d antennas" \
                                  % (len(self.beams), len(self.beam_ids)))
            
            # If there is only one beam, assume it's the same for all ants
            if len(self.beams) == 1:
                beam = self.beams[0]
                self.beams = [beam for b in self.beam_ids]

        # Convert some arguments to simpler forms for vis_cpu.
        self.freqs = self.uvdata.freq_array[0]
        
        # Get antpos for active antennas only
        #self.antpos = self.uvdata.get_ENU_antpos()[0].astype(self._real_dtype)
        self.ant_list = self.uvdata.get_ants() # ordered list of active ants
        self.antpos = []
        _antpos = self.uvdata.get_ENU_antpos()[0].astype(self._real_dtype)
        for ant in self.ant_list:
            # uvdata.get_ENU_antpos() and uvdata.antenna_numbers have entries 
            # for all telescope antennas, even ones that aren't included in the 
            # data_array. This extracts only the data antennas.
            idx = np.where(ant == self.uvdata.antenna_numbers)
            self.antpos.append(_antpos[idx].flatten())
        self.antpos = np.array(self.antpos)
        
        # Validate
        self.validate()
        

    @property
    def lsts(self):
        """
        Sets LSTs from uvdata if not already set.

        Returns
        -------
        array_like
            LSTs of observations. Shape=(NTIMES,).
        """
        try:
            return self.__lsts
        except AttributeError:
            self.__lsts = self.uvdata.lst_array[::self.uvdata.Nbls]

            return self.__lsts

    def validate(self):
        """
        Checks for correct input format.
        """
        super(VisCPU, self).validate()

        # This one in particular requires that every baseline is used!
        N = len(self.uvdata.get_ants())
        
        # N(N-1)/2 unique cross-correlations + N autocorrelations.
        if len(self.uvdata.get_antpairs()) != N * (N + 1) / 2:
            raise ValueError("VisCPU requires using every pair of antennas, "
                             "but the UVData object does not comply.")

        if (len(self.uvdata.data_array) != len(self.uvdata.get_antpairs())
                * len(self.lsts)):
            raise ValueError("VisCPU requires that every baseline uses the "
                             "same LSTS.")
        
        # Check to make sure enough beams are specified
        if not self.use_pixel_beams:
            for ant in self.ant_list:
                assert len(np.where(self.beam_ids == ant)[0]), \
                       "No beam found for antenna %d" % ant
                       
        
    def get_beam_lm(self):
        """
        Obtain the beam pattern in (l,m) co-ordinates for each antenna.

        Returns
        -------
        array_like
            The beam pattern in (l,m) for each antenna. If `self.polarized=True`, 
            its shape is (NAXES, NFEEDS, NANT, BM_PIX, BM_PIX), otherwise 
            (NANT, BM_PIX, BM_PIX).

        Notes
        -----
            Due to using the verbatim :func:`vis_cpu` function, the beam
            cube must have an entry for each antenna, which is a bit of
            a waste of memory in some cases. If this is changed in the
            future, this method can be modified to only return one
            matrix for each beam.
        """
        return np.asarray([
            conversions.uvbeam_to_lm(
                                self.beams[np.where(self.beam_ids == ant)[0][0]],
                                self.freqs,
                                self.bm_pix,
                                polarized=self.polarized
            ) for ant in self.ant_list
        ])

    def get_diffuse_crd_eq(self):
        """
        Calculate equatorial coords of HEALPix sky pixels (Cartesian).

        Returns
        -------
        array_like of self._real_dtype
            The equatorial co-ordinates of each pixel.
            Shape=(12*NPIX^2, 3).
        """
        diffuse_eq = conversions.healpix_to_crd_eq(self.sky_intensity[0])
        return diffuse_eq.astype(self._real_dtype)

    def get_point_source_crd_eq(self):
        """
        Calculate approximate HEALPix map of point sources.

        Returns
        -------
        array_like
            equatorial coordinates of Healpix pixels, in Cartesian
            system. Shape=(3, NPIX).
        """
        ra, dec = self.point_source_pos.T
        return np.asarray([np.cos(ra)*np.cos(dec), np.cos(dec)*np.sin(ra),
                         np.sin(dec)])

    def get_eq2tops(self):
        """
        Calculate transformations from equatorial to topocentric coords.

        Returns
        -------
        array_like of self._real_dtype
            The set of 3x3 transformation matrices converting equatorial
            to topocenteric co-ordinates at each LST.
            Shape=(NTIMES, 3, 3).
        """

        sid_time = self.lsts
        eq2tops = np.empty((len(sid_time), 3, 3), dtype=self._real_dtype)

        for i, st in enumerate(sid_time):
            dec = self.uvdata.telescope_location_lat_lon_alt[0]
            eq2tops[i] = conversions.eq2top_m(-st, dec)

        return eq2tops

    def _base_simulate(self, crd_eq, I):
        """
        Calls :func:vis_cpu to perform the visibility calculation.
        
        Parameters
        ----------
        crd_eq : array_like
            Rotation matrix to convert between source coords and equatorial 
            coords.
        
        I : array_like
            Flux for each source in each frequency channel.
        
        Returns
        -------
        array_like of self._complex_dtype
            Visibilities. Shape=self.uvdata.data_array.shape.
        """
        if self.use_gpu and self.polarized:
            raise NotImplementedError("use_gpu not currently supported if "
                                      "polarized=True")
            
        # Setup MPI info if enabled
        if self.mpi_comm is not None:
            myid = self.mpi_comm.Get_rank()
            nproc = self.mpi_comm.Get_size()
        
        # Convert equatorial to topocentric coords
        eq2tops = self.get_eq2tops()
        
        # Get pixelized beams if required
        if self.use_pixel_beams:
            beam_lm = self.get_beam_lm()
            if not self.polarized:
                beam_lm = beam_lm[np.newaxis,np.newaxis,:,:,:]
        else:
            beam_list = [self.beams[np.where(self.beam_ids == ant)[0][0]] 
                         for ant in self.ant_list]
        
        # Get required pols and map them to the right output index
        if self.polarized:
            avail_pols = {'nn': (0,0), 'ne': (0,1), 'en': (1,0), 'ee': (1,1)}
        else:
            avail_pols = {'ee': (1,1),} # only xx = ee
        
        req_pols = []
        for pol in self.uvdata.polarization_array:
            
            # Get x_orientation
            x_orient = self.uvdata.x_orientation
            if x_orient is None:
                self.uvdata.x_orientation = 'e' # set in UVData object
                x_orient = 'e' # default to east
            
            # Get polarization strings in terms of n/e feeds
            polstr = pyuvdata.utils.polnum2str(pol, 
                                               x_orientation=x_orient).lower()
            
            # Check if polarization can be formed
            if polstr not in avail_pols.keys():
                raise KeyError("Simulation UVData object expecting polarization"
                               " '%s', but only polarizations %s can be formed." 
                               % (polstr, list(avail_pols.keys())))
            
            # If polarization can be formed, specify which is which in the 
            # output polarization_array (ordered list)
            req_pols.append(avail_pols[polstr])
        
        # Empty visibility array
        visfull = np.zeros_like(self.uvdata.data_array,
                                dtype=self._complex_dtype)
        
        for i, freq in enumerate(self.freqs):
            
            # Divide tasks between MPI workers if needed
            if self.mpi_comm is not None:
                if i % nproc != myid: continue
            
            if self.use_pixel_beams:
                # Use pixelized primary beams
                vis = self._vis_cpu(
                    antpos=self.antpos,
                    freq=freq,
                    eq2tops=eq2tops,
                    crd_eq=crd_eq,
                    I_sky=I[i],
                    bm_cube=beam_lm[:,:,:,i],
                    precision=self._precision,
                    polarized=self.polarized
                )
            else:
                # Use UVBeam objects directly
                vis = self._vis_cpu(
                    antpos=self.antpos,
                    freq=freq,
                    eq2tops=eq2tops,
                    crd_eq=crd_eq,
                    I_sky=I[i],
                    beam_list=beam_list,
                    precision=self._precision,
                    polarized=self.polarized
                )
            
            # Assign simulated visibilities to UVData data_array
            if self.polarized:
                indices = np.triu_indices(vis.shape[3])
                for p, pidxs in enumerate(req_pols):
                    p1, p2 = pidxs
                    vis_upper_tri = vis[p1,p2,:,indices[0],indices[1]]
                    visfull[:,0,i,p] = vis_upper_tri.flatten()
                    # Shape: (Nblts, Nspws, Nfreqs, Npols)
            else:
                # Only one polarization (vis is returned without first 2 dims)
                indices = np.triu_indices(vis.shape[1])
                vis_upper_tri = vis[:, indices[0], indices[1]]
                visfull[:,0,i,0] = vis_upper_tri.flatten()
        
        # Reduce visfull array if in MPI mode
        if self.mpi_comm is not None:
            from mpi4py.MPI import SUM
            _visfull = np.zeros(visfull.shape, dtype=visfull.dtype)
            self.mpi_comm.Reduce(visfull, _visfull, op=SUM, root=0)
            if myid == 0:
                return _visfull
            else:
                return 0 # workers return 0
            
        return visfull

    def _simulate_diffuse(self):
        """
        Simulate diffuse sources.

        Returns
        -------
        array_like
            Visibility from point sources.
            Shape=self.uvdata.data_array.shape.
        """
        crd_eq = self.get_diffuse_crd_eq()
        # Multiply intensity by pix area because the algorithm doesn't.
        return self._base_simulate(
            crd_eq,
            self.sky_intensity * healpy.nside2pixarea(self.nside)
        )

    def _simulate_points(self):
        """
        Simulate point sources.

        Returns
        -------
        array_like
            Visibility from diffuse sources.
            Shape=self.uvdata.data_array.shape.
        """
        crd_eq = self.get_point_source_crd_eq()
        return self._base_simulate(crd_eq, self.point_source_flux)

    def _simulate(self):
        """
        Simulate diffuse and point sources.

        Returns
        -------
        array_like
            Visibility from all sources.
            Shape=self.uvdata.data_array.shape.
        """
        vis = 0
        if self.sky_intensity is not None:
            vis += self._simulate_diffuse()
        if self.point_source_flux is not None:
            vis += self._simulate_points()
        return vis


def vis_cpu(antpos, freq, eq2tops, crd_eq, I_sky, bm_cube=None, beam_list=None,
            precision=1, polarized=False):
    """
    Calculate visibility from an input intensity map and beam model.

    Provided as a standalone function.

    Parameters
    ----------
    antpos : array_like
        Antenna position array. Shape=(NANT, 3).
    
    freq : float
        Frequency to evaluate the visibilities at [GHz].
    
    eq2tops : array_like
        Set of 3x3 transformation matrices converting equatorial
        coordinates to topocentric at each
        hour angle (and declination) in the dataset.
        Shape=(NTIMES, 3, 3).
    
    crd_eq : array_like
        Equatorial coordinates of Healpix pixels, in Cartesian system.
        Shape=(3, NPIX).
    
    I_sky : array_like
        Intensity distribution on the sky,
        stored as array of Healpix pixels. Shape=(NPIX,).
    
    bm_cube : array_like, optional
        Pixelized beam maps for each antenna. Shape=(NANT, BM_PIX, BM_PIX).
    
    beam_list : list of UVBeam, optional
        If specified, evaluate primary beam values directly using UVBeam 
        objects instead of using pixelized beam maps (`bm_cube` will be ignored 
        if `beam_list` is not None).
    
    precision : int, optional
        Which precision level to use for floats and complex numbers. 
        Allowed values:
            - 1: float32, complex64
            - 2: float64, complex128
        Default: 1.
    
    polarized : bool, optional
        Whether to simulate a full polarized response in terms of nn, ne, en, 
        ee visibilities.
        
        If False, a single Jones matrix element will be used, corresponding to 
        the (phi, e) element, i.e. the [0,0,1] component of the beam returned 
        by its `interp()` method.
        
        See Eq. 6 of Kohn+ (arXiv:1802.04151) for notation.
        Default: False.
    
    Returns
    -------
    vis : array_like, complex
        Simulated visibilities. If `polarized = True`, the output will have 
        shape (NAXES, NFEED, NTIMES, NANTS, NANTS), otherwise it will have 
        shape (NTIMES, NANTS, NANTS).
    """
    assert precision in (1,2)
    if precision == 1:
        real_dtype=np.float32
        complex_dtype=np.complex64
    else:
        real_dtype=np.float64
        complex_dtype=np.complex128
    
    if bm_cube is None and beam_list is None:
        raise RuntimeError("One of bm_cube/beam_list must be specified")
    if bm_cube is not None and beam_list is not None:
        raise RuntimeError("Cannot specify both bm_cube and beam_list")

    nant, ncrd = antpos.shape
    assert ncrd == 3, "antpos must have shape (NANTS, 3)."
    ntimes, ncrd1, ncrd2 = eq2tops.shape
    assert ncrd1 == 3 and ncrd2 == 3, "eq2tops must have shape (NTIMES, 3, 3)."
    ncrd, npix = crd_eq.shape
    assert ncrd == 3, "crd_eq must have shape (3, NPIX)."
    assert I_sky.ndim == 1 and I_sky.shape[0] == npix, \
        "I_sky must have shape (NPIX,)."
    
    if beam_list is None:
        bm_pix = bm_cube.shape[-1]
        assert bm_cube.shape == (
            nant,
            bm_pix,
            bm_pix,
        ), "bm_cube must have shape (NANTS, BM_PIX, BM_PIX)."
    else:
        assert len(beam_list) == nant, "beam_list must have length nant"

    # Intensity distribution (sqrt) and antenna positions. Does not support
    # negative sky.
    Isqrt = np.sqrt(I_sky).astype(real_dtype)
    antpos = antpos.astype(real_dtype)

    ang_freq = 2 * np.pi * freq
    
    # Specify number of polarizations (axes/feeds)
    if polarized:
        nax = nfeed = 2
    else:
        nax = nfeed = 1
    
    # Empty arrays: beam pattern, visibilities, delays, complex voltages.
    A_s = np.empty((nax, nfeed, nant, npix), dtype=real_dtype)
    vis = np.empty((nax, nfeed, ntimes, nant, nant), dtype=complex_dtype)
    tau = np.empty((nant, npix), dtype=real_dtype)
    v = np.empty((nant, npix), dtype=complex_dtype)
    crd_eq = crd_eq.astype(real_dtype)
    
    # Precompute splines is using pixelized beams
    if beam_list is None:
        bm_pix_x = np.linspace(-1, 1, bm_pix)
        bm_pix_y = np.linspace(-1, 1, bm_pix)
        
        # Construct splines for each polarization (pol. vector axis + feed) and 
        # antenna. The `splines` list has shape (Naxes, Nfeeds, Nants).
        splines = []
        for p1 in range(nax):
            spl_axes = []
            for p2 in range(nfeed):
                spl_feeds = []
                
                # Loop over antennas
                for i in range(nant):
                    # Linear interpolation of primary beam pattern.
                    spl = RectBivariateSpline(bm_pix_y, bm_pix_x, 
                                              bm_cube[p1,p2,i], 
                                              kx=1, ky=1)
                    spl_feeds.append(spl)
                spl_axes.append(spl_feeds)
            splines.append(spl_axes)
            
    # Loop over time samples
    for t, eq2top in enumerate(eq2tops.astype(real_dtype)):
        tx, ty, tz = crd_top = np.dot(eq2top, crd_eq)
        
        # Primary beam response
        if beam_list is None:
            # Primary beam pattern using pixelized primary beam
            for i in range(nant):
                # Extract requested polarizations
                for p1 in range(nax):
                    for p2 in range(nfeed):
                        A_s[p1,p2,i] = splines[p1,p2,i](ty, tx, grid=False)
        else:
            # Primary beam pattern using direct interpolation of UVBeam object
            az, za = conversions.lm_to_az_za(tx, ty)       
            for i in range(nant):
                interp_beam = beam_list[i].interp(az, za, np.atleast_1d(freq))[0]
                
                if polarized:
                    A_s[:,:,i] = interp_beam[:,0,:,0,:] # spw=0 and freq=0
                else:
                    A_s[:,:,i] = interp_beam[0,0,1,:,:] # (phi, e) == 'xx' component
        
        # Horizon cut
        A_s = np.where(tz > 0, A_s, 0)

        # Calculate delays, where tau = (b * s) / c
        np.dot(antpos, crd_top, out=tau)
        tau /= c.value
        
        # Component of complex phase factor for one antenna 
        # (actually, b = (antpos1 - antpos2) * crd_top / c; need dot product 
        # below to build full phase factor for a given baseline)
        np.exp(1.j * (ang_freq * tau), out=v)
        
        # Complex voltages.
        v *= Isqrt

        # Compute visibilities using product of complex voltages (upper triangle).
        # Input arrays have shape (Nax, Nfeed, [Nants], Npix
        for i in range(len(antpos)):
            vis[:, :, t, i:i+1, i:] = np.einsum(
                                        'ijln,jkmn->iklm',
                                        A_s[:,:,i:i+1].conj() \
                                        * v[np.newaxis,np.newaxis,i:i+1].conj(), 
                                        A_s[:,:,i:] \
                                        * v[np.newaxis,np.newaxis,i:],
                                        optimize=True )
    
    # Return visibilities with or without multiple polarization channels
    if polarized:
        return vis
    else:
        return vis[0,0]
