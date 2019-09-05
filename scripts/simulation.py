"""
The purpose of this script is to provide an interface for the user to use 
hera_sim to create, populate, and save to disk a UVData object.
"""
import hera_sim
import sys
import os
import yaml
from bda import bda_tools

# syntax for running from command line:
# python simulation.py path_to_config path_to_save verbose 
# anything else we need to specify?
# notes: 
# path_to_config should be an absolute path to a configuration YAML
# the config YAML will completely specify the instrument and sim component
# parameters (to be applied with defaults and run_sim)
# the decision to perform BDA or not (and which parameters to use) will also
# be made in the config file
#
# path_to_save should be an absolute path specifying the save location with an extension
#
# how to handle verbose?

# get configuration path, simulation parameter path, save path
config = sys.argv[1]

# confirm that config and simulation parameter paths exist
assert os.path.exists(config), \
        "The config file could not be found. Please ensure a path to the file " \
        "exists. The provided path is: {}".format(config)

# load in config
with open(config, 'r') as f:
    yaml_contents = yaml.load(f.read(), Loader=yaml.FullLoader)

# verbose option?
verbose = True if "verbose" in sys.argv else False

# extract parameters for saving to disk
filing_params = yaml_contents["filing"]
outfile = os.path.join(filing_params["outdir"], filing_params["outfile_name"])
save_path = "{}.{}".format(outfile, filing_params["output_format"])
clobber = filing_params["clobber"]

# XXX add a check to make sure the save type is OK?

# determine whether to use season defaults
defaults = yaml_contents.get("defaults", {})
apply_defaults = defaults.pop("apply_defaults", False)
if apply_defaults and defaults:
    if isinstance(defaults["default_config"], str):
        hera_sim.defaults.set(defaults["default_config"])
    elif isinstance(defaults["default_config"], dict):
        hera_sim.defaults.set(**defaults["default_config"])
    else:
        raise ValueError("If you wish to override function defaults, then " \
                "you must do so by either specifying a path to the default " \
                "configuration file or specifying an appropriately formatted " \
                "dictionary of default values.")


# TODO: figure out how to specify antennas
# extract instrument parameters
antennas = sim_params.pop("antennas")
instrument_params = {"antennas" : antennas}
for parameter in ("freq", "time", ):
    for key, value in yaml_contents[parameter].items():
        instrument_params[key] = value

sim = hera_sim.Simulator(**instrument_params)

# extract simulation parameters
sim_params = {}
for component in ("systematics", "analysis", ):
    for key, value in yaml_contents[component].items():
        sim_params[key] = value
# now run the simulation
sim.run_sim(**sim_params)

# figure out whether or not to do BDA, and do it if so
bda_params = yaml_contents.get("bda", {})
apply_bda = bda_params.pop("apply_bda", False)

if apply_bda:
    sim.data = bda_tools.apply_bda(sim.data, **bda_params)

# save the simulation
# note that we may want to allow the functionality for the user to choose some
# kwargs to pass to the write method
sim.write_data(save_path, 
               file_type=filing_params["output_format"],
               **filing_params["kwargs"])

# should be done now

