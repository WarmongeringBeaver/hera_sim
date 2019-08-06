"""
This tests the Simulator object and associated utilities. It does *not*
check for correctness of individual models, as they should be tested
elsewhere.
"""

import shutil
import tempfile
import sys
from os import path

import numpy as np
from nose.tools import raises, assert_raises

from hera_sim.foregrounds import diffuse_foreground
from hera_sim.noise import thermal_noise, HERA_Tsky_mdl
from hera_sim.simulate import Simulator, VersionError
from hera_sim.data import DATA_PATH


def create_sim(autos=False):
    return Simulator(
        n_freq=10,
        n_times=20,
        antennas={
            0: (20.0, 20.0, 0),
            1: (50.0, 50.0, 0)
        },
        no_autos=not autos
    )


# @raises(ValueError)
# def test_wrong_antpairs():
#     Simulator(
#         n_freq=10,
#         n_times=20,
#         antennas={
#             0: (20.0, 20.0, 0),
#             1: (50.0, 50.0, 0)
#         },
#     )
#
#
# @raises(KeyError)
# def test_bad_antpairs():
#     Simulator(
#         n_freq=10,
#         n_times=20,
#         antennas={
#             0: (20.0, 20.0, 0),
#             1: (50.0, 50.0, 0)
#         },
#         antpairs=[(2, 2)]
#     )


def test_from_empty():
    sim = create_sim()

    assert sim.data.data_array.shape == (20, 1, 10, 1)
    assert np.all(np.isclose(sim.data.data_array, 0))


def test_add_with_str():
    sim = create_sim()
    sim.add_eor("noiselike_eor")
    assert not np.all(np.isclose(sim.data.data_array, 0))


def test_add_with_builtin():
    sim = create_sim()
    sim.add_foregrounds(diffuse_foreground, Tsky_mdl=HERA_Tsky_mdl['xx'])
    assert not np.all(np.isclose(sim.data.data_array, 0))


def test_add_with_custom():
    sim = create_sim()

    def custom_noise(**kwargs):
        vis = thermal_noise(**kwargs)
        return 2 * vis

    sim.add_noise(custom_noise)
    assert not np.all(np.isclose(sim.data.data_array, 0))


def test_io():
    sim = create_sim()

    # Create a temporary directory to write stuff to (for python 3 this is much easier)
    direc = tempfile.mkdtemp()

    sim.add_foregrounds("pntsrc_foreground")
    sim.add_gains()

    print(sim.data.antenna_names)
    sim.write_data(path.join(direc, 'tmp_data.uvh5'))

    sim2 = Simulator(
        data=path.join(direc, 'tmp_data.uvh5')
    )

    assert np.all(sim.data.data_array == sim2.data.data_array)

    with assert_raises(ValueError):
        sim.write_data(path.join(direc, 'tmp_data.bad_extension'), file_type="bad_type")

    # delete the tmp
    shutil.rmtree(direc)


@raises(AttributeError)
def test_wrong_func():
    sim = create_sim()

    sim.add_eor("noiselike_EOR")  # wrong function name


@raises(TypeError)
def test_wrong_arguments():
    sim = create_sim()
    sim.add_foregrounds(diffuse_foreground, what=HERA_Tsky_mdl['xx'])


def test_other_components():
    sim = create_sim(autos=True)

    sim.add_xtalk('gen_whitenoise_xtalk', bls=[(0, 1, 'xx')])
    sim.add_xtalk('gen_cross_coupling_xtalk', bls=[(0, 1, 'xx')])
    sim.add_sigchain_reflections(ants=[0])

    assert np.all(np.isclose(sim.data.data_array,  0))

    sim.add_rfi("rfi_stations")

    assert not np.all(np.isclose(sim.data.data_array,  0))


def test_not_add_vis():
    sim = create_sim()
    vis = sim.add_eor("noiselike_eor", add_vis=False)

    assert np.all(np.isclose(sim.data.data_array,  0))

    assert not np.all(np.isclose(vis, 0))

    assert "noiselike_eor" not in sim.data.history


def test_adding_vis_but_also_returning():
    sim = create_sim()
    vis = sim.add_eor("noiselike_eor", ret_vis=True)

    assert not np.all(np.isclose(vis, 0))
    np.testing.assert_array_almost_equal(vis, sim.data.data_array)

    vis = sim.add_foregrounds("diffuse_foreground", Tsky_mdl=HERA_Tsky_mdl['xx'], ret_vis=True)
    np.testing.assert_array_almost_equal(vis, sim.data.data_array, decimal=5)

def test_returning_vis_and_cmp():
    sim = create_sim()
    vis, eor = sim.add_eor("noiselike_eor", ret_vis=True, ret_cmp=True)
    
    # these should be identical
    assert np.all(vis==eor)

    # now do gains
    new_vis, gains = sim.add_gains(ret_vis=True, ret_cmp=True)

    # check that the behavior is as expected
    assert np.all(np.isclose(new_vis, sim.data.data_array - vis))
    assert np.all(np.isclose(sim.data.data_array, eor*gains))

# need python 3.4 or newer to use run_sim
if sys.version_info.major < 3 or \
   sys.version_info.major > 3 and sys.version_info.minor < 4:
    @raises(VersionError)
    def test_run_sim():
        sim_params = {}
        sim = create_sim()
        sim.run_sim(**sim_params)
else:
    def test_run_sim():
        # choose some simulation components
        sim_params = {
                "diffuse_foreground": {"Tsky_mdl":HERA_Tsky_mdl['xx']},
                "pntsrc_foreground": {"nsrcs":500, "Smin":0.1},
                "noiselike_eor": {"eor_amp":3e-2},
                "thermal_noise": {"Tsky_mdl":HERA_Tsky_mdl['xx'], "inttime":8.59},
                "rfi_scatter": {"chance":0.99, "strength":5.7, "std":2.2},
                "rfi_impulse": {"chance":0.99, "strength":17.22},
                "rfi_stations": {},
                "rfi_dtv": {},
                "gains": {"gain_spread":0.05},
                "sigchain_reflections": {"amp":[0.5,0.5],
                                         "dly":[14,7],
                                         "phs":[0.7723,3.2243]},
                "gen_whitenoise_xtalk": {"amplitude":1.2345} 
                }

        sim = create_sim()
    
        # let's get the simulation components
        sim_components = sim.run_sim(ret_sim_components=True, **sim_params)

        assert not np.all(np.isclose(sim.data.data_array, 0))

        # make sure that we can reconstruct the simulated vis with components
        # first note which ones are multiplicative
        is_mult = ('gains', 'sigchain_reflections')

        # initialize an array to store the visibilities in
        vis = np.zeros(sim.data.data_array.shape, dtype=np.complex)
        
        # loop over the components
        for model, component in sim_components.items():
            if model in is_mult:
                vis *= component
            else:
                vis += component

        # these might not be *exactly* equal, but they should be close
        assert np.all(np.isclose(vis, sim.data.data_array))

        # instantiate a mock simulation file
        tmp_sim_file = tempfile.mkstemp()[1]
        # write something to it
        with open(tmp_sim_file, 'w') as sim_file:
            sim_file.write("""
                diffuse_foreground: 
                    Tsky_mdl: 
                        file: {}/HERA_Tsky_Reformatted.npz
                        pol: yy
                pntsrc_foreground: 
                    nsrcs: 500
                    Smin: 0.1
                noiselike_eor: 
                    eor_amp: 0.03
                gains: 
                    gain_spread: 0.05
                gen_cross_coupling_xtalk: 
                    amp: 0.225
                    dly: 13.2
                    phs: 2.1123
                thermal_noise: 
                    Tsky_mdl: 
                        file: {}/HERA_Tsky_Reformatted.npz
                        pol: xx
                    inttime: 9.72
                rfi_scatter: 
                    chance: 0.99
                    strength: 5.7
                    std: 2.2
                    """.format(DATA_PATH, DATA_PATH))
        sim = create_sim(autos=True)
        sim.run_sim(tmp_sim_file)
        assert not np.all(np.isclose(sim.data.data_array, 0))

    @raises(AssertionError)
    def test_run_sim_both_args():
        # make a temporary test file
        tmp_sim_file = tempfile.mkstemp()[1]
        with open(tmp_sim_file, 'w') as sim_file:
            sim_file.write("""
                pntsrc_foreground:
                    nsrcs: 5000
                    """)
        sim_params = {"diffuse_foreground": {"Tsky_mdl":HERA_Tsky_mdl['xx']} }
        sim = create_sim()
        sim.run_sim(tmp_sim_file, **sim_params)

    @raises(AssertionError)
    def test_run_sim_bad_param_key():
        bad_key = {"something": {"something else": "another different thing"} }
        sim = create_sim()
        sim.run_sim(**bad_key)

    @raises(AssertionError)
    def test_run_sim_bad_param_value():
        bad_value = {"diffuse_foreground": 13}
        sim = create_sim()
        sim.run_sim(**bad_value)

    @raises(SystemExit)
    def test_bad_yaml_config():
        # make a bad config file
        tmp_sim_file = tempfile.mkstemp()[1]
        with open(tmp_sim_file, 'w') as sim_file:
            sim_file.write("""
                this:
                    is: a
                     bad: file
                     """)
        sim = create_sim()
        sim.run_sim(tmp_sim_file)

    @raises(KeyError)
    def test_bad_tsky_key():
        # make a config file with no file key for Tsky_mdl
        tmp_sim_file = tempfile.mkstemp()[1]
        with open(tmp_sim_file, 'w') as sim_file:
            sim_file.write("""
                diffuse_foreground: 
                    Tsky_mdl: 
                        pol: xx
                        """)
        sim = create_sim()
        sim.run_sim(tmp_sim_file)

    @raises(TypeError)
    def test_bad_tsky_mdl():
        # make a config file where Tsky_mdl is not a dict
        tmp_sim_file = tempfile.mkstemp()[1]
        with open(tmp_sim_file, 'w') as sim_file:
            sim_file.write("""
                diffuse_foreground:
                    Tsky_mdl: 13
                    """)
        sim = create_sim()
        sim.run_sim(tmp_sim_file)

