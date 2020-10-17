"""
    Using ProteinNet as a guide, this script creates a new dataset that adds sidechain atom
    information.
    It retains data splits, sequences, and masks but recomputes each structure's coordinates so that
    sidechain atoms may be recorded. It also saves the entirety of the data as a single Python
    dictionary.

    Author: Jonathan King
    Date:   July 21, 2019
"""

import argparse
import datetime
import multiprocessing
import pickle
import re
import sys
from glob import glob

import numpy as np
import prody as pr
import tqdm

from sidechainnet.utils.astral_data import ASTRAL_SUMMARY
from sidechainnet.utils.errors import *
from sidechainnet.utils.errors import ERRORS, report_errors
from sidechainnet.utils.measure import GLOBAL_PAD_CHAR, angle_list_to_sin_cos, \
    get_seq_coords_and_angles, no_nans_infs_allzeros
from sidechainnet.utils.parse import get_chain_from_astral_id, parse_astral_summary_file
from sidechainnet.utils.sequence import bin_sequence_data

MAX_SEQ_LEN = 10_000
VALID_SPLITS = [10, 20, 30, 40, 50, 70, 90]
ASTRAL_ID_MAPPING = parse_astral_summary_file(ASTRAL_SUMMARY.splitlines())
del ASTRAL_SUMMARY


def download_sidechain_data(pnids, sidechainnet_out_dir, casp_version, training_set,
                            limit, proteinnet_in):
    """Downloads the sidechain data for the corresponding ProteinNet IDs.

    Args:
        pnids: List of ProteinNet IDs to download sidechain data for
        sidechainnet_out_dir: Path to directory for saving sidechain data
        casp_version: A string that describes the CASP version i.e. 'casp7'
        training_set: Which thinning of ProteinNet to extract (30, 50, 90, etc.)
        limit: An integer describing maximum number of proteins to process
        proteinnet_in: A string representing the path to processed proteinnet.

    Returns:
        sc_data: Python dictionary `{pnid: {...}, ...}`
    """
    from sidechainnet.utils.organize import load_data, save_data, organize_data
    # Initalize directories.
    global PROTEINNET_IN_DIR
    PROTEINNET_IN_DIR = proteinnet_in
    output_name = f"sidechain-only_{casp_version}_{training_set}.pkl"
    output_path = os.path.join(sidechainnet_out_dir, output_name)
    if not os.path.exists(sidechainnet_out_dir):
        os.mkdir(sidechainnet_out_dir)

    # Simply load sidechain data if it has already been processed.
    if os.path.exists(output_path):
        print(f"Sidechain information already preprocessed ({output_path}).")
        return load_data(output_path), output_path

    # Clean up any error logs that have been left-over from previous runs.
    if os.path.exists("errors"):
        for file in glob('errors/*.txt'):
            os.remove(file)

    # Download the sidechain data as a dictionary and report errors.
    sc_data, pnids_errors = get_sidechain_data(pnids, limit)
    save_data(sc_data, output_path)
    report_errors(pnids_errors, total_pnids=len(pnids[:limit]))

    # Clean up working directory
    for file in glob('*.cif'):
        os.remove(file)

    return sc_data, output_path


def get_sidechain_data(pnids, limit):
    """Acquires sidechain data for specified ProteinNet IDs.
    Args:
        pnids: List of ProteinNet IDs to download data for.
        limit: Number of IDs to process (use small value for debugging).

    Returns:
        Dictionary mapping pnids to sidechain data. For example:

        {"1a9u_A_2": {"seq": "MRYK...",
                      "ang": np.ndarray(...),
                        ...
                      }}
        Also returns a list of tuples of (pnid, error_code) for those pnids
        that failed to download.
    """
    with multiprocessing.Pool(multiprocessing.cpu_count()) as p:
        results = list(
            tqdm.tqdm(p.imap(process_id, pnids[:limit]),
                      total=len(pnids[:limit]),
                      dynamic_ncols=True))
    all_errors = []
    all_data = dict()
    with open("errors/MODIFIED_MODEL_WARNING.txt", "a") as model_warning_file:
        for pnid, r in results:
            if type(r) == int:
                all_errors.append((pnid, r))
                continue
            if "msg" in r and r["msg"] == "MODIFIED_MODEL":
                model_warning_file.write(f"{pnid}\n")
                del r["msg"]
            all_data[pnid] = r
    return all_data, all_errors


def process_id(pnid):
    """ Creates dictionary of sidechain data for a single ProteinNet ID.

    For a single ProteinNet ID i.e. ('1A9U_1_A'), fetches that PDB chain
    from the PDB and extracts its angles, coordinates, and sequence. Missing
    data is padded with GLOBAL_PAD_CHARs.
    Args:
        pnid: A ProteinNet ID, i.e. '1A9U_1_A'.

    Returns:
        Dictionary of relevant data for that ID.

    """
    message = None
    pnid_type = determine_pnid_type(pnid)
    chain = get_chain_from_proteinnetid(pnid, pnid_type)
    if not chain:
        return pnid, ERRORS["NONE_STRUCTURE_ERRORS"]
    elif type(chain) == tuple and type(chain[1]) == int:
        # This indicates there was an issue parsing the chain
        return pnid, chain[1]
    elif type(chain) == tuple and chain[1] == "MODIFIED_MODEL":
        message = "MODIFIED_MODEL"
        chain = chain[0]
    try:
        dihedrals_coords_sequence = get_seq_coords_and_angles(chain)
    except NonStandardAminoAcidError:
        return pnid, ERRORS["NSAA_ERRORS"]
    except NoneStructureError:
        return pnid, ERRORS["NONE_STRUCTURE_ERRORS"]
    except ContigMultipleMatchingError:
        return pnid, ERRORS["MULTIPLE_CONTIG_ERRORS"]
    except ShortStructureError:
        return pnid, ERRORS["SHORT_ERRORS"]
    except MissingAtomsError:
        return pnid, ERRORS["MISSING_ATOMS_ERROR"]
    except SequenceError:
        print("Not fixed.", pnid)
        return pnid, ERRORS["SEQUENCE_ERRORS"]

    # If we've made it this far, we can unpack the data and return it
    dihedrals, coords, sequence = dihedrals_coords_sequence
    data = {"ang": dihedrals, "crd": coords, "seq": sequence}
    if message:
        data["msg"] = message
    return pnid, data


def determine_pnid_type(pnid):
    """Returns the 'type' of a ProteinNet ID (i.e. train, valid, test, ASTRAL).

    Args:
        pnid: ProteinNet ID string.

    Returns:
        The 'type' of ProteinNet ID as a string.
    """
    if "TBM#" in pnid or "FM#" in pnid or "TBM-hard" in pnid or "FM-hard" in pnid:
        return "test"

    if pnid.count("_") == 1:
        is_astral = "_astral"
    else:
        is_astral = ""

    if "#" in pnid:
        return "valid" + is_astral
    else:
        return "train" + is_astral


def get_chain_from_trainid(pnid):
    """ Return a ProDy chain object for a ProteinNet ID. Assumes train/valid ID.

    Args:
        pnid: ProteinNet ID

    Returns:
        ProDy chain object corresponding to ProteinNet ID.
    """
    modified_model_number = False
    # Try parsing the ID as a PDB ID. If it fails, assume it's an ASTRAL ID.
    try:
        pdbid, chnum, chid = pnid.split("_")
        chnum = int(chnum)
        # If this is a validation set pnid, separate the annotation from the ID
        if "#" in pdbid:
            pdbid = pdbid.split("#")[1]
    except ValueError:
        try:
            pdbid, astral_id = pnid.split("_")
            return get_chain_from_astral_id(astral_id.replace("-", "_"),
                                            ASTRAL_ID_MAPPING)
        except KeyError:
            return pnid, ERRORS["MISSING_ASTRAL_IDS"]
        except ValueError:
            return pnid, ERRORS["FAILED_ASTRAL_IDS"]
        except:
            return pnid, ERRORS["FAILED_ASTRAL_IDS"]

    # Continue loading the chain, given the PDB ID
    use_pdb = True
    try:
        chain = pr.parsePDB(pdbid, chain=chid, model=chnum)
        if not chain:
            chain = pr.parseCIF(pdbid, chain=chid, model=chnum)
            use_pdb = False
    # If the file is too large, then we can download the CIF instead
    except OSError:
        try:
            chain = pr.parseCIF(pdbid, chain=chid, model=chnum)
            use_pdb = False
        except IndexError:
            try:
                chain = pr.parseCIF(pdbid, chain=chid, model=1)
                use_pdb = False
                modified_model_number = True
            except Exception as e:
                print(e)
                return pnid, ERRORS["PARSING_ERROR_OSERROR"]
        except Exception as e:  # EOFERROR
            print(e)
            return pnid, ERRORS["PARSING_ERROR_OSERROR"]
    except AttributeError:
        return pnid, ERRORS["PARSING_ERROR_ATTRIBUTE"]
    except (pr.proteins.pdbfile.PDBParseError, IndexError):
        # For now, if the requested coordinate set doesn't exist, then we will
        # default to using the only (first) available coordinate set
        struct = pr.parsePDB(pdbid, chain=chid) if use_pdb else pr.parseCIF(pdbid,
                                                                            chain=chid)
        if struct and chnum > 1:
            try:
                chain = pr.parsePDB(pdbid, chain=chid, model=1)
                modified_model_number = True
            except:
                return pnid, ERRORS["PARSING_ERROR"]
        else:
            return pnid, ERRORS["PARSING_ERROR"]
    except Exception as e:
        print(e)
        return pnid, ERRORS["UNKNOWN_EXCEPTIONS"]

    if chain is None:
        return pnid, ERRORS["NONE_CHAINS"]

    if modified_model_number:
        return chain, "MODIFIED_MODEL"

    return chain


def get_chain_from_testid(pnid):
    """ Returns a ProDy chain object for a test pnid. Requires local file.

    Args:
        pnid: ProteinNet ID. Must refer to a test-set record.

    Returns:
        ProDy chain object.
    """

    category, caspid = pnid.split("#")
    try:
        chain = pr.parsePDB(os.path.join(PROTEINNET_IN_DIR, "targets", caspid + ".pdb"))
    except AttributeError:
        return pnid, ERRORS["TEST_PARSING_ERRORS"]
    try:
        assert chain.numChains() == 1
    except:
        print("Only a single chain should be parsed from the CASP target PDB.")
        return pnid, ERRORS["TEST_PARSING_ERRORS"]
    return chain


def get_chain_from_proteinnetid(pnid, pnid_type):
    """Returns a ProDy chain for a given pnid.

    Args:
        pnid_type:
        pnid: ProteinNet ID

    Returns:
        ProDy chain object.
    """
    # If the ProteinNet ID is from the test set
    if pnid_type == "test":
        chain = get_chain_from_testid(pnid)
    # If the ProteinNet ID is from the train or validation set
    else:
        chain = get_chain_from_trainid(pnid)
    return chain


def unpack_processed_results(results, pnids):
    """
    Given an iterable of processed results containing angles, sequences, and PDB IDs,
    this function separates out the components (sequences as one-hot vectors, angle matrices,
    and PDB IDs) iff all were successfully preprocessed.
    """
    all_ohs = []
    all_angs = []
    all_crds = []
    all_ids = []
    c = 0
    for r, pnid in zip(results, pnids):
        if type(r) == int:
            # PDB failed to download
            ERRORS.count(r, pnid)
            continue
        ang, coords, seq, i = r
        if no_nans_infs_allzeros(ang) and no_nans_infs_allzeros(coords):
            all_ohs.append(seq)
            all_angs.append(ang)
            all_crds.append(coords)
            all_ids.append(i)
            c += 1
    print(f"{(c * 100) / len(results):.1f}% of chains parsed. ({c}/{len(results)})")
    return all_ohs, all_angs, all_crds, all_ids


def add_proteinnetID_to_idx_mapping(data):
    """
    Given an already processes ProteinNet data dictionary, this function adds
    a mapping from ProteinNet ID to the subset and index number where that
    protein can be looked up in the current dictionary. Useful if you'd like
    to quickly extract a certain protein.
    """
    d = {}
    for subset in ["train", "test"] + [f"valid-{split}" for split in VALID_SPLITS]:
        for idx, pnid in enumerate(data[subset]["ids"]):
            d[pnid] = {"subset": subset, "idx": idx}

    data["pnids"] = d
    return data
