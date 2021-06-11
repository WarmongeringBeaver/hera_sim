"""Re-imagining of the simulation module."""

import functools
import inspect
import os
import sys
import warnings
import yaml
import time
from pathlib import Path

import numpy as np
from cached_property import cached_property
from pyuvdata import UVData
from astropy import constants as const
from typing import Type, Union, Tuple, Sequence, Optional, Dict

from . import io
from . import utils
from .defaults import defaults
from . import __version__
from .components import SimulationComponent, get_model, list_all_components


# Define some commonly used types for typing purposes.
AntPairPol = Tuple[int, int, str]
AntPair = Tuple[int, int]
AntPol = Tuple[int, str]
Component = Union[str, Type[SimulationComponent], SimulationComponent]


# wrapper for the run_sim method, necessary for part of the CLI
def _generator_to_list(func, *args, **kwargs):
    @functools.wraps(func)
    def new_func(*args, **kwargs):
        result = list(func(*args, **kwargs))
        return None if result == [] else result

    return new_func


class Simulator:
    """Simulate visibilities and/or instrumental effects for an entire array.

    Parameters
    ----------
    data
        ``pyuvdata.UVData`` object to use for the simulation or path to a
        UVData-supported file.
    defaults_config
        Path to defaults configuraiton, seasonal keyword, or configuration
        dictionary for setting default simulation parameters. See tutorial
        on setting defaults for further information.
    redundancy_tol
        Position tolerance for finding redundant groups, in meters. Default is
        1 meter.
    kwargs
        Parameters to use for initializing UVData object if none is provided.
        If ``data`` is a file path, then these parameters are used when reading
        the file. Otherwise, the parameters are used in creating a ``UVData``
        object using :func:`io.empty_uvdata`.

    Attributes
    ----------
    data : ``pyuvdata.UVData``
        Object containing simulated visibilities and metadata.
    extras : dict
        Dictionary to use for storing extra parameters.
    antpos : dict
        Dictionary pairing antenna numbers to ENU positions in meters.
    lsts : np.ndarray of float
        Observed LSTs in radians.
    freqs : np.ndarray of float
        Observed frequencies in GHz.
    times : np.ndarray of float
        Observed times in JD.
    pols : list of str
        Polarization strings.
    red_grps : list of list of int
        Redundant baseline groups. Each entry is a list containing the baseline
        integer for each member of that redundant group.
    red_vecs : list of np.ndarray of float
        Average of all the baselines for each redundant group.
    red_lengths : list of float
        Length of each redundant baseline.
    """

    def __init__(
        self,
        *,
        data: Optional[Union[str, UVData]] = None,
        defaults_config: Optional[Union[str, Dict]] = None,
        redundancy_tol: float = 1.0,
        **kwargs,
    ):
        # TODO: add ability for user to specify parameter names to look for on
        # parsing call signature
        # Create some utility dictionaries.
        self._components = {}
        self._seeds = {}
        self._antpairpol_cache = {}
        self._filter_cache = {
            "delay": {},
            "fringe": {},
        }

        # apply and activate defaults if specified
        if defaults_config:
            self.apply_defaults(defaults_config)

        # actually initialize the UVData object stored in self.data
        self._initialize_data(data, **kwargs)
        self._calculate_reds(tol=redundancy_tol)
        self.extras = self.data.extra_keywords
        for param in ("Ntimes", "Nfreqs", "Nblts", "Npols", "Nbls"):
            setattr(self, param, getattr(self.data, param))
        self.Nants = len(self.antpos)
        self.get_data = self.data.get_data
        self.get_flags = self.data.get_flags

    @cached_property
    def antpos(self):
        # TODO: docstring
        """"""
        antpos, ants = self.data.get_ENU_antpos(pick_data_ants=True)
        return dict(zip(ants, antpos))

    @cached_property
    def lsts(self):
        """Observed Local Sidereal Times in radians."""
        # This process retrieves the unique LSTs while respecting phase wraps.
        unique_lsts, inverse_inds, counts = np.unique(
            self.data.lst_array, return_inverse=True, return_counts=True
        )
        return unique_lsts[inverse_inds[:: counts[0]]]

    @cached_property
    def freqs(self):
        """Frequencies in GHz."""
        return np.unique(self.data.freq_array) / 1e9

    @cached_property
    def times(self):
        """Simulation times in JD."""
        return np.unique(self.data.time_array)

    @cached_property
    def pols(self):
        """Array of polarization strings."""
        return self.data.get_pols()

    def apply_defaults(self, config, refresh=True):
        """
        Apply the provided default configuration.

        Equivalent to calling :meth:`hera_sim.defaults` with the same parameters.
        See :meth:`hera_sim.defaults.set` documentation for further details.
        """
        defaults.set(config, refresh=refresh)

    def calculate_filters(
        self,
        *,
        delay_filter_kwargs: Optional[Dict[str, Union[float, str]]] = None,
        fringe_filter_kwargs: Optional[Dict[str, Union[float, str, np.ndarray]]] = None,
    ):
        """
        Pre-compute fringe-rate and delay filters for the entire array.

        Parameters
        ----------
        delay_filter_kwargs
            Extra parameters necessary for generating a delay filter. See
            :func:`utils.gen_delay_filter` for details.
        fringe_filter_kwargs
            Extra parameters necessary for generating a fringe filter. See
            :func:`utils.gen_fringe_filter` for details.
        """
        delay_filter_kwargs = delay_filter_kwargs or {}
        fringe_filter_kwargs = fringe_filter_kwargs or {}
        self._calculate_delay_filters(**delay_filter_kwargs)
        self._calculate_fringe_filters(**fringe_filter_kwargs)

    def add(
        self,
        component: Component,
        *,
        add_vis: bool = True,
        ret_vis: bool = False,
        seed: Optional[Union[str, int]] = None,
        vis_filter: Optional[Sequence] = None,
        name: Optional[str] = None,
        **kwargs,
    ) -> Optional[Union[np.ndarray, Dict[int, np.ndarray]]]:
        """
        Simulate an effect then apply and/or return the result.

        Parameters
        ----------
        component
            Effect to be simulated. This can either be an alias of the effect,
            or the class (or instance thereof) that simulates the effect.
        add_vis
            Whether to apply the effect to the simulated data. Default is True.
        ret_vis
            Whether to return the simulated effect. Nothing is returned by default.
        seed
            How to seed the random number generator. Can either directly provide
            a seed as an integer, or use one of the supported keywords. See
            :meth:`_seed_rng` docstring for information on accepted values.
            Default is to use a seed based on the current random state.
        vis_filter
            Iterable specifying which antennas/polarizations for which the effect
            should be simulated. See documentation of :meth:`_apply_filter` for
            details of supported formats and functionality.
        component_name
            Name to use when recording the parameters used for simulating the effect.
            Default is to use the name of the class used to simulate the effect.
        **kwargs
            Optional keyword arguments for the provided ``component``.

        Returns
        -------
        effect
            The simulated effect; only returned if ``ret_vis`` is set to ``True``.
            If the simulated effect is multiplicative, then a dictionary mapping
            antenna numbers to the per-antenna effect (as a ``np.ndarray``) is
            returned. Otherwise, the effect for the entire array is returned with
            the same structure as the ``pyuvdata.UVData.data_array`` that the
            data is stored in.
        """
        # Obtain a callable reference to the simulation component model.
        model = self._get_component(component)
        model_key = name if name else self._get_model_name(component)
        if not isinstance(model, SimulationComponent):
            model = model(**kwargs)
        self._sanity_check(model)  # Check for component ordering issues.
        self._antpairpol_cache[model_key] = []  # Initialize this model's cache.
        if seed is None:
            # Ensure we can recover the data later via ``get``
            seed = int(np.random.get_state()[1][0])

        # Simulate the effect by iterating over baselines and polarizations.
        data = self._iteratively_apply(
            model,
            add_vis=add_vis,
            ret_vis=ret_vis,
            vis_filter=vis_filter,
            antpairpol_cache=self._antpairpol_cache[model_key],
            seed=seed,
            **kwargs,
        )  # This is None if ret_vis is False

        if add_vis:
            # Record the component simulated and the parameters used.
            if defaults._override_defaults:
                for param in getattr(model, "kwargs", {}):
                    if param not in kwargs and param in defaults():
                        kwargs[param] = defaults(param)
            self._update_history(model, **kwargs)
            # Record the random state in case no seed was specified.
            # This ensures that the component can be recovered later.
            kwargs["seed"] = seed
            self._update_seeds(model_key)
            if vis_filter is not None:
                kwargs["vis_filter"] = vis_filter
            self._components[model_key] = kwargs
            self._components[model_key]["alias"] = component
        else:
            del self._antpairpol_cache[model_key]

        return data

    def get(
        self,
        component: Component,
        key: Optional[Union[int, str, AntPair, AntPairPol]] = None,
    ) -> Union[np.ndarray, Dict[int, np.ndarray]]:
        """
        Retrieve an effect that was previously simulated.

        Parameters
        ----------
        component
            Effect that is to be retrieved. See :meth:`add` for more details.
        key
            Key for retrieving simulated effect. Possible choices are as follows:
                An integer may specify either a single antenna (for per-antenna
                effects) or be a ``pyuvdata``-style baseline integer.
                A string specifying a polarization can be used to retrieve the
                effect for every baseline for the specified polarization.
                A length-2 tuple of integers can be used to retrieve the effect
                for that baseline for all polarizations.
                A length-3 tuple specifies a particular baseline and polarization
                for which to retrieve the effect.
            Not specifying a key results in the effect being returned for all
            baselines (or antennas, if the effect is per-antenna) and polarizations.

        Returns
        -------
        effect
            The simulated effect appropriate for the provided key. Return type
            depends on the effect being simulated and the provided key. See the
            tutorial Jupyter notebook for the ``Simulator`` for example usage.
        """
        # Retrieve the model and verify it has been simulated.
        if component in self._components:
            model = self._get_component(self._components[component]["alias"])
            model_key = component
        else:
            model = self._get_component(component)
            model_key = self._get_model_name(component)
            if model_key not in self._components:
                raise ValueError("The provided component has not yet been simulated.")

        # Parse the key and verify that it's properly formatted.
        ant1, ant2, pol = self._parse_key(key)
        self._validate_get_request(model, ant1, ant2, pol)

        # Prepare to re-simulate the effect.
        kwargs = self._components[model_key].copy()
        kwargs.pop("alias")  # To handle multiple instances of simulating an effect.
        seed = kwargs.pop("seed", None)
        vis_filter = kwargs.pop("vis_filter", None)
        if not isinstance(model, SimulationComponent):
            model = model(**kwargs)

        if model.is_multiplicative:
            # We'll get a dictionary back, so the handling is different.
            gains = self._iteratively_apply(
                model,
                add_vis=False,
                ret_vis=True,
                seed=seed,
                vis_filter=vis_filter,
                **kwargs,
            )
            if ant1 is not None:
                if pol:
                    return gains[(ant1, pol)]
                return {key: gain for key, gain in gains.items() if ant1 in key}
            else:
                if pol:
                    return {key: gain for key, gain in gains.items() if pol in key}
                return gains

        # Specifying neither antenna implies the full array's data is desired.
        if ant1 is None and ant2 is None:
            # Simulate the effect
            data = self._iteratively_apply(
                model,
                add_vis=False,
                ret_vis=True,
                seed=seed,
                vis_filter=vis_filter,
                antpairpol_cache=None,
                **kwargs,
            )

            # Trim the data if a specific polarization is requested.
            if pol is None:
                return data
            pol_ind = self.pols.index(pol)
            return data[:, 0, :, pol_ind]

        # We're only simulating for a particular baseline.
        # First, find out if it needs to be conjugated.
        try:
            blt_inds = self.data.antpair2ind(ant1, ant2)
            if blt_inds.size == 0:
                raise ValueError
            conj_data = False
        except ValueError:
            blt_inds = self.data.antpair2ind(ant2, ant1)
            conj_data = True

        # We've got three different seeding cases to work out.
        if seed == "initial":
            # Initial seeding means we need to do the whole array.
            data = self._iteratively_apply(
                model,
                add_vis=False,
                ret_vis=True,
                seed=seed,
                vis_filter=vis_filter,
                antpairpol_cache=None,
                **kwargs,
            )[blt_inds, 0, :, :]
            if conj_data:  # pragma: no cover
                data = np.conj(data)
            if pol is None:
                return data
            pol_ind = self.data.get_pols().index(pol)
            return data[..., pol_ind]
        elif seed == "redundant":
            if conj_data:
                self._seed_rng(seed, model, ant2, ant1, pol)
            else:
                self._seed_rng(seed, model, ant1, ant2, pol)
        elif seed is not None:
            self._seed_rng(seed, model, ant1, ant2, pol)

        # Prepare the model parameters, then simulate and return the effect.
        if pol is None:
            data_shape = (self.lsts.size, self.freqs.size, len(self.pols))
            pols = self.pols
            return_slice = (slice(None),) * 3
        else:
            data_shape = (self.lsts.size, self.freqs.size, 1)
            pols = (pol,)
            return_slice = (slice(None), slice(None), 0)
        data = np.zeros(data_shape, dtype=np.complex)
        for i, _pol in enumerate(pols):
            args = self._initialize_args_from_model(model)
            args = self._update_args(args, ant1, ant2, pol)
            args.update(kwargs)
            if conj_data:
                self._seed_rng(seed, model, ant2, ant1, _pol)
            else:
                self._seed_rng(seed, model, ant1, ant2, _pol)
            data[..., i] = model(**args)
        if conj_data:
            data = np.conj(data)
        return data[return_slice]

    def plot_array(self):
        """Generate a plot of the array layout in ENU coordinates."""
        import matplotlib.pyplot as plt

        fig = plt.figure(figsize=(10, 8))
        ax = fig.add_subplot(1, 1, 1)
        ax.set_xlabel("East Position [m]", fontsize=12)
        ax.set_ylabel("North Position [m]", fontsize=12)
        ax.set_title("Array Layout", fontsize=12)
        dx = 0.25
        for ant, pos in self.antpos.items():
            ax.plot(pos[0], pos[1], color="k", marker="o")
            ax.text(pos[0] + dx, pos[1] + dx, ant)
        return fig

    def refresh(self):
        """Refresh the Simulator object.

        This zeros the data array, resets the history, and clears the
        instance's _components dictionary.
        """
        self.data.data_array = np.zeros(self.data.data_array.shape, dtype=complex)
        self.data.history = ""
        self._components.clear()
        self._antpairpol_cache.clear()
        self._seeds.clear()
        self.extras.clear()

    def write(self, filename, save_format="uvh5", **kwargs):
        # TODO: docstring
        """"""
        try:
            getattr(self.data, f"write_{save_format}")(filename, **kwargs)
        except AttributeError:
            raise ValueError(
                "The save_format must correspond to a write method in UVData."
            )

    # TODO: Determine if we want to provide the user the option to retrieve
    # simulation components as a return value from run_sim. Remove the
    # _generator_to_list wrapper if we do not make that a feature.
    @_generator_to_list
    def run_sim(self, sim_file=None, **sim_params):
        # TODO: docstring
        """"""
        # make sure that only sim_file or sim_params are specified
        if not (bool(sim_file) ^ bool(sim_params)):
            raise ValueError(
                "Either an absolute path to a simulation configuration "
                "file or a dictionary of simulation parameters may be "
                "passed, but not both. Please only pass one of the two."
            )

        # read the simulation file if provided
        if sim_file is not None:
            with open(sim_file, "r") as config:
                try:
                    sim_params = yaml.load(config.read(), Loader=yaml.FullLoader)
                except Exception:
                    raise IOError("The configuration file was not able to be loaded.")

        # loop over the entries in the configuration dictionary
        for component, params in sim_params.items():
            # make sure that the parameters are a dictionary
            if not isinstance(params, dict):
                raise TypeError(
                    "The parameters for {component} are not formatted "
                    "properly. Please ensure that the parameters for "
                    "each component are specified using a "
                    "dictionary.".format(component=component)
                )

            # add the component to the data
            value = self.add(component, **params)

            # if the user wanted to return the data, then
            if value is not None:
                yield component, value

    def chunk_sim_and_save(
        self,
        save_dir,
        ref_files=None,
        Nint_per_file=None,
        prefix=None,
        sky_cmp=None,
        state=None,
        filetype="uvh5",
        clobber=True,
    ):
        """
        Chunk a simulation in time and write to disk.

        This function is a thin wrapper around :func:`io.chunk_sim_and_save`;
        please see that function's documentation for more information.
        """
        io.chunk_sim_and_save(
            self.data,
            save_dir,
            ref_files=ref_files,
            Nint_per_file=Nint_per_file,
            prefix=prefix,
            sky_cmp=sky_cmp,
            state=state,
            filetype=filetype,
            clobber=clobber,
        )
        return

    # -------------- Legacy Functions -------------- #
    # TODO: write a deprecated wrapper function
    def add_eor(self, model, **kwargs):
        """
        Add an EoR-like model to the visibilities. See :meth:`add` for
        more details.
        """
        return self.add(model, **kwargs)

    def add_foregrounds(self, model, **kwargs):
        """
        Add foregrounds to the visibilities. See :meth:`add` for
        more details.
        """

        return self.add(model, **kwargs)

    def add_noise(self, model, **kwargs):
        """
        Add thermal noise to the visibilities. See :meth:`add` for
        more details.
        """
        return self.add(model, **kwargs)

    def add_rfi(self, model, **kwargs):
        """Add RFI to the visibilities. See :meth:`add` for more details."""
        return self.add(model, **kwargs)

    def add_gains(self, **kwargs):
        """
        Apply bandpass gains to the visibilities. See :meth:`add` for
        more details.
        """
        return self.add("gains", **kwargs)

    def add_sigchain_reflections(self, ants=None, **kwargs):
        """
        Apply reflection gains to the visibilities. See :meth:`add` for
        more details.
        """
        if ants is not None:
            kwargs.update(vis_filter=ants)
        return self.add("reflections", **kwargs)

    def add_xtalk(self, model="gen_whitenoise_xtalk", bls=None, **kwargs):
        """Add crosstalk to the visibilities. See :meth:`add` for more details."""
        if bls is not None:
            kwargs.update(vis_filter=bls)
        return self.add(model, **kwargs)

    @staticmethod
    def _apply_filter(vis_filter, ant1, ant2, pol):
        """Determine whether to filter the visibility for (ant1, ant2, pol).

        Functionally, ``vis_filter`` specifies which (ant1, ant2, pol) tuples
        will have a simulated effect propagated through the ``_iteratively_apply``
        method. ``vis_filter`` acts as a logical equivalent of a passband filter.

        Parameters
        ----------
        vis_filter
            Either a polarization string, antenna number, baseline, antpairpol
            (baseline + polarization), collection of antenna numbers and/or
            polarization strings, or collection of such keys.
        ant1, ant2, pol
            Baseline + polarization to compare against the provided filter.

        Returns
        -------
        apply_filter
            False if the provided antpairpol satisfies any of the keys provided
            in ``vis_filter``; True otherwise. See examples for details.

        Examples
        --------
        ``vis_filter`` = (0,)
        returns: False for any baseline including antenna 0
            -> only baselines including antenna 0 have a simulated effect applied.

        ``vis_filter`` = ('xx',)
        returns: False if ``pol == "xx"`` else True
            -> only polarization "xx" has a simulated effect applied.

        ``vis_filter`` = (0, 1, 'yy')
        returns: False if ``(ant1, ant2, pol) in [(0, 1, 'yy'), (1, 0, 'yy)]``
            -> only baseline (0,1), or its conjugate, with polarization 'yy' will
            have a simulated effect applied.
        """
        # If multiple complex keys are passed, do this recursively...
        multikey = any(isinstance(key, (list, tuple)) for key in vis_filter)
        if multikey:
            apply_filter = [
                Simulator._apply_filter(key, ant1, ant2, pol) for key in vis_filter
            ]
            return all(apply_filter)  # and approve if just one key fits.
        elif all(item is None for item in vis_filter):
            # Support passing a list of None.
            return False
        elif len(vis_filter) == 1:
            # For now, assume a string specifies a polarization.
            if isinstance(vis_filter[0], str):
                return not pol == vis_filter[0]
            # Otherwise, assume that this specifies an antenna.
            else:
                return not vis_filter[0] in (ant1, ant2)
        elif len(vis_filter) == 2:
            # TODO: This will need to be updated when we support ant strings.
            # Three cases: two pols; an ant+pol; a baseline.
            # If it's two polarizations, then make sure this pol is one of them.
            if all(isinstance(key, str) for key in vis_filter):
                return pol not in vis_filter
            # Otherwise this is straightforward.
            else:
                return not all(key in (ant1, ant2, pol) for key in vis_filter)
        elif len(vis_filter) == 3:
            # Assume it's a proper antpairpol.
            return not (
                vis_filter == [ant1, ant2, pol] or vis_filter == [ant2, ant1, pol]
            )
        else:
            # Assume it's some list of antennas/polarizations.
            pols = []
            ants = []
            for key in vis_filter:
                if isinstance(key, str):
                    pols.append(key)
                elif type(key) is int:
                    ants.append(key)
            # We want polarization and ant1 or ant2 in the filter.
            # This would be used in simulating e.g. a few feeds that have an
            # abnormally high system temperature.
            return not (pol in pols and (ant1 in ants or ant2 in ants))

    def _calculate_reds(self, tol=1.0):
        """Calculate redundant groups and populate class attributes."""
        groups, centers, lengths = self.data.get_redundancies(tol=tol)
        self.red_grps = groups
        self.red_vecs = centers
        self.red_lengths = lengths

    def _calculate_delay_filters(
        self,
        *,
        standoff: float = 0.0,
        delay_filter_type: Optional[str] = "gauss",
        min_delay: Optional[float] = None,
        max_delay: Optional[float] = None,
        normalize: Optional[float] = None,
    ):
        """
        Calculate delay filters for each redundant group.

        Parameters
        ----------
        standoff
            Extra extent in delay that the filter extends out to in order to
            allow for suprahorizon emission. Should be specified in nanoseconds.
            Default buffer is zero.
        delay_filter_type
            String specifying the filter profile. See :func:`utils.gen_delay_filter`
            for details.
        min_delay
            Minimum absolute delay of the filter, in nanoseconds.
        max_delay
            Maximum absolute delay of the filter, in nanoseconds.
        normalize
            Normalization of the filter such that the output power is the product
            of the input power and the normalization factor.

        See Also
        --------
        :func:`utils.gen_delay_filter`
        """
        # Note that this is not the most efficient way of caching the filters;
        # however, this is algorithmically very simple--just use one filter per
        # redundant group. This could potentially be improved in the future,
        # but it should work fine for our purposes.
        for red_grp, bl_len in zip(self.red_grps, self.red_lengths):
            bl_len_ns = bl_len / const.c.to("m/ns").value
            bl_int = sorted(red_grp)[0]
            delay_filter = utils.gen_delay_filter(
                self.freqs,
                bl_len_ns,
                standoff=standoff,
                delay_filter_type=delay_filter_type,
                min_delay=min_delay,
                max_delay=max_delay,
                normalize=normalize,
            )
            self._filter_cache["delay"][bl_int] = delay_filter

    def _calculate_fringe_filters(
        self,
        *,
        fringe_filter_type: Optional[str] = "tophat",
        **filter_kwargs,
    ):
        """
        Calculate fringe-rate filters for all baselines.

        Parameters
        ----------
        fringe_filter_type
            The fringe-rate filter profile.
        filter_kwargs
            Other parameters necessary for specifying the filter. These
            differ based on the filter profile.

        See Also
        --------
        :func:`utils.gen_fringe_filter`
        """
        # This uses the same simplistic approach as the delay filter
        # calculation does--just do one filter per redundant group.
        for red_grp, (blx, bly, blz) in zip(self.red_grps, self.red_vecs):
            ew_bl_len_ns = blx / const.c.to("m/ns").value
            bl_int = sorted(red_grp)[0]
            fringe_filter = utils.gen_fringe_filter(
                self.lsts,
                self.freqs,
                ew_bl_len_ns,
                fringe_filter_type=fringe_filter_type,
                **filter_kwargs,
            )
            self._filter_cache["fringe"][bl_int] = fringe_filter

    def _initialize_data(
        self,
        data: Optional[Union[str, Path, UVData]],
        **kwargs,
    ):
        """
        Initialize the ``data`` attribute with a ``UVData`` object.

        Parameters
        ----------
        data
            Either a ``UVData`` object or a path-like object to a file
            that can be loaded into a ``UVData`` object. If not provided,
            then sufficient keywords for initializing a ``UVData`` object
            must be provided. See :func:`io.empty_uvdata` for more
            information on which keywords are needed.

        Raises
        ------
        TypeError
            If the provided value for ``data`` is not an object that can
            be cast to a ``UVData`` object.
        """
        if data is None:
            self.data = io.empty_uvdata(**kwargs)
        elif isinstance(data, (str, Path)):
            self.data = self._read_datafile(data, **kwargs)
            self.data.extra_keywords["data_file"] = data
        elif isinstance(data, UVData):
            self.data = data
        else:
            raise TypeError(
                "data type not understood. Only a UVData object or a path to "
                "a UVData-compatible file may be passed as the data parameter. "
                "Otherwise, keywords must be provided to build a UVData object."
            )

    def _initialize_args_from_model(self, model):
        """
        Retrieve the LSTs and/or frequencies required for a model.

        Parameters
        ----------
        model: callable
            Model whose argspec is to be inspected and recovered.

        Returns
        -------
        model_params: dict
            Dictionary mapping positional argument names to either an
            ``inspect._empty`` object or the relevant parameters pulled
            from the ``Simulator`` object. The only parameters that are
            not ``inspect._empty`` are "lsts" and "freqs", should they
            appear in the model's argspec.

        Examples
        --------
        Suppose we have the following function::
            def func(freqs, ants, other=None):
                pass
        The returned object would be a dictionary with keys ``freqs`` and
        ``ants``, with the value for ``freqs`` being ``self.freqs`` and
        the value for ``ants`` being ``inspect._empty``. Since ``other``
        has a default value, it will not be in the returned dictionary.
        """
        model_params = self._get_model_parameters(model)
        model_params = {k: v for k, v in model_params.items() if v is inspect._empty}

        # Pull the LST and frequency arrays if they are required.
        args = {
            param: getattr(self, param)
            for param in model_params
            if param in ("lsts", "freqs")
        }

        model_params.update(args)

        return model_params

    def _iterate_antpair_pols(self):
        """Loop through all baselines and polarizations."""
        for ant1, ant2, pol in self.data.get_antpairpols():
            blt_inds = self.data.antpair2ind((ant1, ant2))
            pol_ind = self.data.get_pols().index(pol)
            if blt_inds.size:
                yield ant1, ant2, pol, blt_inds, pol_ind

    # TODO: think about how to streamline this algorithm and make it more readable
    # In particular, make the logic for adding/returning the effect easier to follow.
    def _iteratively_apply(
        self,
        model: SimulationComponent,
        *,
        add_vis: bool = True,
        ret_vis: bool = False,
        seed: Optional[Union[str, int]] = None,
        vis_filter: Optional[Sequence] = None,
        antpairpol_cache: Optional[Sequence[AntPairPol]] = None,
        **kwargs,
    ):
        """
        Simulate an effect for an entire array.

        This method loops over every baseline and polarization in order
        to simulate the effect ``model`` for the full array. The result
        is optionally applied to the simulation's data and/or returned.

        Parameters
        ----------
        model
            Callable model used to simulate an effect.
        add_vis
            Whether to apply the effect to the simulation data. Default
            is to apply the effect.
        ret_vis
            Whether to return the simulated effect. Default is to not
            return the effect. Type of returned object depends on whether
            the effect is multiplicative or not.
        seed
            Either an integer specifying the seed to be used in setting
            the random state, or one of a select few keywords. Default
            is to use the current random state. See :meth:`_seed_rng`
            for descriptions of the supported seeding modes.
        vis_filter
            List of antennas, baselines, polarizations, antenna-polarization
            pairs, or antpairpols for which to simulate the effect. This
            specifies which of the above the effect is to be simulated for,
            and anything that does not meet the keys specified in this list
            does not have the effect applied to it. See :meth:`_apply_filter`
            for more details.
        antpairpol_cache
            List of (ant1, ant2, pol) tuples specifying which antpairpols have
            already had the effect simulated. Not intended for use by the
            typical end-user.
        kwargs
            Extra parameters passed to ``model``.

        Returns
        -------
        effect: np.ndarray or dict
            The simulated effect. Only returned if ``ret_vis`` is set to True.
            If the effect is *not* multiplicative, then the returned object
            is an ndarray; otherwise, a dictionary mapping antenna numbers
            to ndarrays is returned.
        """
        # There's nothing to do if we're neither adding nor returning.
        if not add_vis and not ret_vis:
            warnings.warn(
                "You have chosen to neither add nor return the effect "
                "you are trying to simulate, so nothing will be "
                "computed. This warning was raised for the model: "
                "{model}".format(model=self._get_model_name(model))
            )
            return

        # Initialize the antpairpol cache if we need to.
        if antpairpol_cache is None:
            antpairpol_cache = []

        # Pull relevant parameters from Simulator.
        # Also make placeholders for antenna/baseline dependent parameters.
        base_args = self._initialize_args_from_model(model)

        # Get a copy of the data array.
        data_copy = self.data.data_array.copy()

        # Pull useful auxilliary parameters.
        is_multiplicative = getattr(model, "is_multiplicative", None)
        is_smooth_in_freq = getattr(model, "is_smooth_in_freq", True)
        if is_multiplicative is None:
            warnings.warn(
                "You are attempting to compute a component but have "
                "not specified an ``is_multiplicative`` attribute for "
                "the component. The component will be added under "
                "the assumption that it is *not* multiplicative."
            )
            is_multiplicative = False

        # Pre-simulate gains.
        if is_multiplicative:
            gains = {}
            args = self._update_args(base_args)
            args.update(kwargs)
            for pol in self.data.get_feedpols():
                if seed:
                    seed = self._seed_rng(seed, model, pol=pol)
                polarized_gains = model(**args)
                for ant, gain in polarized_gains.items():
                    gains[(ant, pol)] = gain

        # Determine whether to use cached filters, and which ones to use if so.
        model_kwargs = getattr(model, "kwargs", {})
        use_cached_filters = any("filter" in key for key in model_kwargs)
        get_delay_filter = is_smooth_in_freq and "delay_filter_kwargs" not in kwargs
        get_delay_filter &= bool(self._filter_cache["delay"])
        get_fringe_filter = "fringe_filter_kwargs" not in kwargs
        get_fringe_filter &= bool(self._filter_cache["fringe"])
        use_cached_filters &= get_delay_filter or get_fringe_filter

        # Iterate over the array and simulate the effect as-needed.
        for ant1, ant2, pol, blt_inds, pol_ind in self._iterate_antpair_pols():
            # Determine whether or not to filter the result.
            apply_filter = self._apply_filter(
                utils._listify(vis_filter), ant1, ant2, pol
            )
            if apply_filter:
                continue

            # Check if this antpairpol or its conjugate have been simulated.
            bl_in_cache = (ant1, ant2, pol) in antpairpol_cache
            conj_in_cache = (ant2, ant1, pol) in antpairpol_cache

            # Seed the random number generator.
            key = (ant2, ant1, pol) if conj_in_cache else (ant1, ant2, pol)
            seed = self._seed_rng(seed, model, *key)

            # Prepare the actual arguments to be used.
            use_args = self._update_args(base_args, ant1, ant2, pol)
            use_args.update(kwargs)
            if use_cached_filters:
                filter_kwargs = self._get_filters(
                    ant1,
                    ant2,
                    get_delay_filter=get_delay_filter,
                    get_fringe_filter=get_fringe_filter,
                )
                use_args.update(filter_kwargs)

            # Cache simulated antpairpols if not filtered out.
            if not (bl_in_cache or conj_in_cache or apply_filter):
                antpairpol_cache.append((ant1, ant2, pol))

            # Check whether we're simulating a gain or a visibility.
            if is_multiplicative:
                # Calculate the complex gain, but only apply it if requested.
                gain = gains[(ant1, pol[0])] * np.conj(gains[(ant2, pol[1])])
                data_copy[blt_inds, 0, :, pol_ind] *= gain
            else:
                # I don't think this will ever be executed, but just in case...
                if conj_in_cache and seed is None:  # pragma: no cover
                    conj_blts = self.data.antpair2ind((ant2, ant1))
                    vis = (data_copy - self.data.data_array)[
                        conj_blts, 0, :, pol_ind
                    ].conj()
                else:
                    vis = model(**use_args)

                # and add it in
                data_copy[blt_inds, 0, :, pol_ind] += vis

        # return the component if desired
        # this is a little complicated, but it's done this way so that
        # there aren't *three* copies of the data array floating around
        # this is to minimize the potential of triggering a MemoryError
        if ret_vis:
            # return the gain dictionary if gains are simulated
            if is_multiplicative:
                return gains
            data_copy -= self.data.data_array
            # the only time we're allowed to have add_vis be False is
            # if ret_vis is True, and nothing happens if both are False
            # so this is the *only* case where we'll have to reset the
            # data array
            if add_vis:
                self.data.data_array += data_copy
            # otherwise return the actual visibility simulated
            return data_copy
        else:
            self.data.data_array = data_copy

    @staticmethod
    def _read_datafile(datafile, **kwargs):
        # TODO: docstring
        """"""
        uvd = UVData()
        uvd.read(datafile, read_data=True, **kwargs)
        return uvd

    def _seed_rng(self, seed, model, ant1=None, ant2=None, pol=None):
        # TODO: docstring
        """"""
        if seed is None:
            return
        if type(seed) is int:
            np.random.seed(seed)
            return
        if not isinstance(seed, str):
            raise TypeError(
                "The seeding mode must be specified as a string or integer. "
                "If an integer is provided, then it will be used as the seed."
            )
        if seed == "redundant":
            if ant1 is None or ant2 is None:
                raise TypeError(
                    "A baseline must be specified in order to "
                    "seed by redundant group."
                )
            # Determine the key for the redundant group this baseline is in.
            bl_int = self.data.antnums_to_baseline(ant1, ant2)
            key = (next(reds for reds in self.red_grps if bl_int in reds)[0],)
            if pol:
                key += (pol,)
            # seed the RNG accordingly
            np.random.seed(self._get_seed(model, key))
            return "redundant"
        elif seed == "once":
            # this option seeds the RNG once per iteration of
            # _iteratively_apply, using the same seed every time
            # this is appropriate for antenna-based gains (where the
            # entire gain dictionary is simulated each time), or for
            # something like PointSourceForeground, where objects on
            # the sky are being placed randomly
            key = (pol,) if pol else 0
            np.random.seed(self._get_seed(model, key))
            return "once"
        elif seed == "initial":
            # this seeds the RNG once at the very beginning of
            # _iteratively_apply. this would be useful for something
            # like ThermalNoise
            key = (pol,) if pol else -1
            np.random.seed(self._get_seed(model, key))
            return None
        else:
            raise ValueError("Seeding mode not supported.")

    def _update_args(self, args, ant1=None, ant2=None, pol=None):
        """
        Scan the provided arguments and pull data as necessary.

        This method searches the provided dictionary for various positional
        arguments that can be determined by data stored in the ``Simulator``
        instance. Please refer to the source code to see what argument
        names are searched for and how their values are obtained.

        Parameters
        ----------
        args: dict
            Dictionary mapping names of positional arguments to either
            a value pulled from the ``Simulator`` instance or an
            ``inspect._empty`` object. See .. meth: _initialize_args_from_model
            for details on what to expect (these two methods are always
            called in conjunction with one another).
        ant1: int, optional
            Required parameter if an autocorrelation visibility or a baseline
            vector is in the keys of ``args``.
        ant2: int, optional
            Required parameter if a baseline vector is in the keys of ``args``.
        pol: str, optional
            Polarization string. Currently not used.
        """
        # Helper function for getting the correct parameter name
        def key(requires):
            return list(args)[requires.index(True)]

        # find out what needs to be added to args
        # for antenna-based gains
        _requires_ants = [param.startswith("ant") for param in args]
        requires_ants = any(_requires_ants)
        # for sky components
        _requires_bl_vec = [param.startswith("bl") for param in args]
        requires_bl_vec = any(_requires_bl_vec)
        # for cross-coupling xtalk
        _requires_vis = [param.find("vis") != -1 for param in args]
        requires_vis = any(_requires_vis)

        # check if this is an antenna-dependent quantity; should
        # only ever be true for gains (barring future changes)
        if requires_ants:
            new_param = {key(_requires_ants): self.antpos}
        # check if this is something requiring a baseline vector
        # current assumption is that these methods require the
        # baseline vector to be provided in nanoseconds
        elif requires_bl_vec:
            bl_vec = self.antpos[ant2] - self.antpos[ant1]
            bl_vec_ns = bl_vec * 1e9 / const.c.value
            new_param = {key(_requires_bl_vec): bl_vec_ns}
        # check if this is something that depends on another
        # visibility. as of now, this should only be cross coupling
        # crosstalk
        elif requires_vis:
            print(f"{ant1}, {ant2}, {pol}")
            autovis = self.data.get_data(ant1, ant1, pol)
            new_param = {key(_requires_vis): autovis}
        else:
            new_param = {}
        # update appropriately and return
        use_args = args.copy()
        use_args.update(new_param)

        # there should no longer be any unspecified, required parameters
        # so this *shouldn't* error out
        use_args = {
            key: value
            for key, value in use_args.items()
            if not type(value) is inspect.Parameter
        }

        if any([val is inspect._empty for val in use_args.values()]):
            warnings.warn(
                "One of the required parameters was not extracted. "
                "Please check that the parameters for the model you "
                "are trying to add are detectable by the Simulator. "
                "The Simulator will automatically find the following "
                "required parameters: \nlsts \nfreqs \nAnything that "
                "starts with 'ant' or 'bl'\n Anything containing 'vis'."
            )

        return use_args

    def _get_filters(
        self,
        ant1: int,
        ant2: int,
        *,
        get_delay_filter: bool = True,
        get_fringe_filter: bool = True,
    ) -> Dict[str, np.ndarray]:
        """
        Retrieve delay and fringe filters from the cache.

        Parameters
        ----------
        ant1
            First antenna in the baseline.
        ant2
            Second antenna in the baseline.
        get_delay_filter
            Whether to retrieve the delay filter.
        get_fringe_filter
            Whether to retrieve the fringe filter.

        Returns
        -------
        filters
            Dictionary containing the fringe and delay filters that
            have been pre-calculated for the provided baseline.
        """
        filters = {}
        if not get_delay_filter and not get_fringe_filter:
            # Save some CPU cycles.
            return filters
        bl_int = self.data.antnums_to_baseline(ant1, ant2)
        conj_bl_int = self.data.antnums_to_baseline(ant2, ant1)
        is_conj = False
        for red_grp in self.red_grps:
            if bl_int in red_grp:
                key = sorted(red_grp)[0]
                break
            if conj_bl_int in red_grp:
                key = sorted(red_grp)[0]
                is_conj = True
                break
        if get_delay_filter:
            delay_filter = self._filter_cache["delay"][key]
            filters["delay_filter_kwargs"] = {}
            filters["delay_filter_kwargs"]["delay_filter"] = delay_filter
        if get_fringe_filter:
            fringe_filter = self._filter_cache["fringe"][key]
            if is_conj:
                # Fringes are seen to move in the opposite direction.
                fringe_filter = fringe_filter[::-1, :]
            filters["fringe_filter_kwargs"] = {}
            filters["fringe_filter_kwargs"]["fringe_filter"] = fringe_filter
        return filters

    @staticmethod
    def _get_model_parameters(model):
        """Retrieve the full model signature (init + call) parameters."""
        init_params = inspect.signature(model.__class__).parameters
        call_params = inspect.signature(model).parameters
        # this doesn't work correctly if done on one line
        model_params = {}
        for params in (call_params, init_params):
            for parameter, value in params.items():
                model_params[parameter] = value.default
        model_params.pop("kwargs", None)
        return model_params

    @staticmethod
    def _get_component(
        component: [str, Type[SimulationComponent], SimulationComponent]
    ) -> Union[SimulationComponent, Type[SimulationComponent]]:
        """Given an input component, normalize the output to be either a class or instance."""
        if np.issubclass_(component, SimulationComponent):
            return component
        elif isinstance(component, str):
            try:
                return get_model(component)
            except KeyError:
                raise ValueError(
                    f"The model '{component}' does not exist. The following models are "
                    f"available: \n{list_all_components()}."
                )
        elif isinstance(component, SimulationComponent):
            return component
        else:
            raise TypeError(
                "The input type for the component was not understood. "
                "Must be a string, or a class/instance of type 'SimulationComponent'. "
                f"Available component models are:\n{list_all_components()}"
            )

    def _generate_seed(self, model, key):
        # TODO: docstring
        """"""
        model = self._get_model_name(model)
        # for the sake of randomness
        np.random.seed(int(time.time() * 1e6) % 2 ** 32)
        if model not in self._seeds:
            self._seeds[model] = {}
        self._seeds[model][key] = np.random.randint(2 ** 32)

    def _get_seed(self, model, key):
        # TODO: docstring
        """"""
        model = self._get_model_name(model)
        if model not in self._seeds:
            self._generate_seed(model, key)
        # TODO: handle conjugate baselines here instead of other places
        if key not in self._seeds[model]:
            self._generate_seed(model, key)
        return self._seeds[model][key]

    @staticmethod
    def _get_model_name(model):
        """
        Find out the (lowercase) name of a provided model.
        """
        if isinstance(model, str):
            return model
        elif np.issubclass_(model, SimulationComponent):
            return model.__name__
        elif isinstance(model, SimulationComponent):
            return model.__class__.__name__
        else:
            raise TypeError(
                "You are trying to simulate an effect using a custom function. "
                "Please refer to the tutorial for instructions regarding how "
                "to define new simulation components compatible with the Simulator."
            )

    def _parse_key(self, key: Union[int, str, AntPair, AntPairPol]) -> AntPairPol:
        """Convert a key of at-most length-3 to an (ant1, ant2, pol) tuple."""
        if key is None:
            ant1, ant2, pol = None, None, None
        elif np.issubdtype(type(key), int):
            # Figure out if it's an antenna or baseline integer
            if key in self.antpos:
                ant1, ant2, pol = key, None, None
            else:
                ant1, ant2 = self.data.baseline_to_antnums(key)
                pol = None
        elif isinstance(key, str):
            if key.lower() in ("auto", "cross"):
                raise NotImplementedError("Functionality not yet supported.")
            ant1, ant2, pol = None, None, key
        else:
            try:
                iter(key)
                if len(key) not in (2, 3):
                    raise TypeError
            except TypeError:
                raise ValueError(
                    "Key must be an integer, string, antenna pair, or antenna "
                    "pair with a polarization string."
                )
            if len(key) == 2:
                if all(type(val) is int for val in key):
                    ant1, ant2 = key
                    pol = None
                else:
                    ant1, pol = key
                    ant2 = None
            else:
                ant1, ant2, pol = key
        return ant1, ant2, pol

    def _sanity_check(self, model):
        # TODO: docstring
        """"""
        has_data = not np.all(self.data.data_array == 0)
        is_multiplicative = getattr(model, "is_multiplicative", False)
        contains_multiplicative_effect = any(
            self._get_component(component).is_multiplicative
            for component in self._components
        )

        if is_multiplicative and not has_data:
            warnings.warn(
                "You are trying to compute a multiplicative "
                "effect, but no visibilities have been "
                "simulated yet."
            )
        elif not is_multiplicative and contains_multiplicative_effect:
            warnings.warn(
                "You are adding visibilities to a data array "
                "*after* multiplicative effects have been "
                "introduced."
            )

    def _update_history(self, model, **kwargs):
        """
        Record the component simulated and its parameters in the history.
        """
        component = self._get_model_name(model)
        vis_filter = kwargs.pop("vis_filter", None)
        msg = f"hera_sim v{__version__}: Added {component} using parameters:\n"
        for param, value in defaults._unpack_dict(kwargs).items():
            msg += f"{param} = {value}\n"
        if vis_filter is not None:
            msg += "Effect simulated for the following antennas/baselines/pols:\n"
            msg += ", ".join(vis_filter)
        self.data.history += msg

    def _update_seeds(self, model_name=None):
        """Update the seeds in the extra_keywords property."""
        seed_dict = {}
        for component, seeds in self._seeds.items():
            if model_name is not None and component != model_name:
                continue

            if len(seeds) == 1:
                seed = list(seeds.values())[0]
                key = "_".join([component, "seed"])
                seed_dict[key] = seed
            else:
                # This should only be raised for seeding by redundancy.
                # Each redundant group is denoted by the *first* baseline
                # integer for the particular redundant group. See the
                # _generate_redundant_seeds method for reference.
                for bl_int, seed in seeds.items():
                    key = "_".join([component, "seed", str(bl_int)])
                    seed_dict[key] = seed

        # Now actually update the extra_keywords dictionary.
        self.data.extra_keywords.update(seed_dict)

    def _validate_get_request(
        self, model: Component, ant1: int, ant2: int, pol: str
    ) -> None:
        """Verify that the provided antpairpol is appropriate given the model."""
        if getattr(model, "is_multiplicative", False):
            pols = self.data.get_feedpols()
            pol_type = "Feed"
        else:
            pols = self.pols
            pol_type = "Visibility"
        if ant1 is None and ant2 is None:
            if pol is None or pol in pols:
                return
            else:
                raise ValueError(f"{pol_type} polarization {pol} not found.")

        if pol is not None and pol not in pols:
            raise ValueError(f"{pol_type} polarization {pol} not found.")

        if getattr(model, "is_multiplicative", False):
            if ant1 is not None and ant2 is not None:
                raise ValueError(
                    "At most one antenna may be specified when retrieving "
                    "a multiplicative effect."
                )
        else:
            if (ant1 is None) ^ (ant2 is None):
                raise ValueError(
                    "Either no antennas or a pair of antennas must be provided "
                    "when retrieving a non-multiplicative effect."
                )
            if ant1 not in self.antpos or ant2 not in self.antpos:
                raise ValueError("At least one antenna is not in the array layout.")
