""" Utility functions for parsing protein structures. """

import numpy as np
import prody as pr

from sidechainnet.utils.build_info import NUM_ANGLES, NUM_BB_OTHER_ANGLES, NUM_BB_TORSION_ANGLES, NUM_COORDS_PER_RES, SC_BUILD_INFO
from sidechainnet.utils.errors import IncompleteStructureError, MissingAtomsError, NonStandardAminoAcidError, NoneStructureError, SequenceError

GLOBAL_PAD_CHAR = np.nan


def angle_list_to_sin_cos(angs, reshape=True):
    """
    Given a list of angles, returns a new list where those angles have been
    turned into their sines and cosines. If reshape is False, a new dim. is
    added that can hold the sine and cosine of each angle, i.e. (len x #angs)
    -> ( len x #angs x 2). If reshape is true, this last dim. is squashed so
    that the list of angles becomes [cos sin cos sin ...].
    """
    new_list = []
    for a in angs:
        new_mat = np.zeros((a.shape[0], a.shape[1], 2))
        new_mat[:, :, 0] = np.cos(a)
        new_mat[:, :, 1] = np.sin(a)
        if reshape:
            new_list.append(new_mat.reshape(-1, NUM_ANGLES * 2))
        else:
            new_list.append(new_mat)
    return new_list


def check_standard_continuous(residue, prev_res_num):
    """
    Asserts that the residue is standard and that the chain is continuous.
    """
    if not residue.isstdaa:
        raise NonStandardAminoAcidError("Found a non-std AA.")
    if residue.getResnum() != prev_res_num:
        raise IncompleteStructureError("Chain is missing residues.")
    return True


def determine_sidechain_atomnames(_res):
    """
    Given a residue from ProDy, returns a list of sidechain atom names that
    must be recorded.
    """
    if _res.getResname() in SC_BUILD_INFO.keys():
        return SC_BUILD_INFO[_res.getResname()]["atom-names"]
    else:
        raise NonStandardAminoAcidError


def compute_sidechain_dihedrals(residue, prev_residue, next_res):
    """
    Computes all angles to predict for a given residue. If the residue is the
    first in the protein chain, a fictitious C atom is placed before the
    first N. This is used to compute a [ C-1, N, CA, CB] dihedral angle. If
    it is not the first residue in the chain, the previous residue's C is
    used instead. Then, each group of 4 atoms in atom_names is used to
    generate a list of dihedral angles for this residue.
    """
    try:
        torsion_names = SC_BUILD_INFO[residue.getResname()]["torsion-names"]
    except KeyError:
        raise NonStandardAminoAcidError
    if len(torsion_names) == 0:
        return (NUM_ANGLES -
                (NUM_BB_TORSION_ANGLES + NUM_BB_OTHER_ANGLES)) * [GLOBAL_PAD_CHAR]

    # Compute CB dihedral, which may depend on the previous or next residue for placement
    try:
        if prev_residue:
            cb_dihedral = compute_single_dihedral(
                (prev_residue.select("name C"),
                 *(residue.select(f"name {an}") for an in ["N", "CA", "CB"])))
        else:
            cb_dihedral = compute_single_dihedral(
                (next_res.select("name N"),
                 *(residue.select(f"name {an}") for an in ["C", "CA", "CB"])))
    except AttributeError:
        cb_dihedral = GLOBAL_PAD_CHAR

    res_dihedrals = [cb_dihedral]

    for t_name, t_val in zip(torsion_names[1:],
                             SC_BUILD_INFO[residue.getResname()]["torsion-vals"][1:]):
        # Only record torsional angles that are relevant (i.e. not planar).
        # All torsion values that vary are marked with 'p' in SC_BUILD_INFO
        if t_val != "p":
            break
        atom_names = t_name.split("-")
        res_dihedrals.append(
            compute_single_dihedral([residue.select("name " + an) for an in atom_names]))

    return res_dihedrals + (NUM_ANGLES - (NUM_BB_TORSION_ANGLES + NUM_BB_OTHER_ANGLES) -
                            len(res_dihedrals)) * [GLOBAL_PAD_CHAR]


def get_atom_coords_by_names(residue, atom_names):
    """
    Given a ProDy Residue and a list of atom names, this attempts to select
    and return all the atoms. If atoms are not present, it substitutes the
    pad character in lieu of their coordinates.
    """
    coords = []
    pad_coord = np.asarray([GLOBAL_PAD_CHAR] * 3)
    for an in atom_names:
        a = residue.select(f"name {an}")
        if a:
            coords.append(a.getCoords()[0])
        else:
            coords.append(pad_coord)
    return coords


def measure_res_coordinates(_res):
    """
    Given a ProDy residue, measure all relevant coordinates.
    """
    sc_atom_names = determine_sidechain_atomnames(_res)
    bbcoords = get_atom_coords_by_names(_res, ["N", "CA", "C", "O"])
    sccoords = get_atom_coords_by_names(_res, sc_atom_names)
    coord_padding = np.zeros((NUM_COORDS_PER_RES - len(bbcoords) - len(sccoords), 3))
    coord_padding[:] = GLOBAL_PAD_CHAR
    return np.concatenate((np.stack(bbcoords + sccoords), coord_padding))


def empty_coord():
    """
    Return an empty coordinate tensor, representing 1 padding character at
    the residue level.
    """
    coord_padding = np.zeros((NUM_COORDS_PER_RES, 3))
    coord_padding[:] = GLOBAL_PAD_CHAR
    return coord_padding


def empty_ang():
    """
    Return an empty angle tensor, representing 1 padding character at the
    residue level.
    """
    dihe_padding = np.zeros(NUM_ANGLES)
    dihe_padding[:] = GLOBAL_PAD_CHAR
    return dihe_padding


def replace_nonstdaas(residues):
    """Replaces the non-standard Amino Acids in a list with their equivalents.

    Args:
        residues: List of ProDy residues.

    Returns:
        A list of residues where any non-standard residues have been replaced
        with the canonical version (i.e. MSE -> MET, SEP -> SER, etc.)
        See http://prody.csb.pitt.edu/manual/reference/atomic/flags.html
        for a complete list of non-standard amino acids supported here.
        `XAA` is treated as missing.
    """
    replacements = {
        "ASX": "ASP",
        "GLX": "GLU",
        "CSO": "CYS",
        "HIP": "HIS",
        "HSD": "HIS",
        "HSE": "HIS",
        "HSP": "HIS",
        "MSE": "MET",
        "SEC": "CYS",
        "SEP": "SER",
        "TPO": "THR",
        "PTR": "TYR",
        "XLE": "LEU"
    }

    for r in residues:
        rname = r.getResname()
        if rname in replacements.keys():
            r.setResname(replacements[rname])

    return residues


def get_seq_coords_and_angles(chain):
    """Extracts protein sequence, coordinates, and angles from a ProDy chain.

    Args:
        chain: ProDy chain object

    Returns:
        Returns a tuple (angles, coords, sequence) for the protein chain.
        Returns None if the data fails to parse.
        Example angles returned:
            [[phi, psi, omega, ncac, cacn, cnca, chi1, chi2,...chi12],[...] ...]
    """
    chain = chain.select("protein")
    if chain is None:
        raise NoneStructureError
    chain = chain.copy()

    coords = []
    dihedrals = []
    observed_sequence = ""
    all_residues = list(chain.iterResidues())
    if chain.nonstdaa:
        all_residues = replace_nonstdaas(all_residues)
    prev_res = None
    next_res = all_residues[1]

    for res_id, res in enumerate(all_residues):
        if res.getResname() == "XAA":  # Treat unknown amino acid as missing
            continue
        elif not res.stdaa:
            raise NonStandardAminoAcidError

        # Measure basic angles
        bb_angles = measure_phi_psi_omega(res)
        bond_angles = measure_bond_angles(res, res_id, all_residues)

        # Measure sidechain angles
        all_res_angles = bb_angles + bond_angles + compute_sidechain_dihedrals(
            res, prev_res, next_res)

        # Measure coordinates
        rescoords = measure_res_coordinates(res)

        # Update records
        coords.append(rescoords)
        dihedrals.append(all_res_angles)
        prev_res = res
        observed_sequence += res.getSequence()[0]

    dihedrals_np = np.asarray(dihedrals)
    coords_np = np.concatenate(coords)

    if coords_np.shape[0] != len(observed_sequence) * NUM_COORDS_PER_RES:
        print(
            f"Coords shape {coords_np.shape} does not match len(seq)*{NUM_COORDS_PER_RES} = "
            f"{len(observed_sequence) * NUM_COORDS_PER_RES},\nOBS: {observed_sequence}\n{chain}"
        )
        raise SequenceError

    return dihedrals_np, coords_np, observed_sequence


def no_nans_infs_allzeros(matrix):
    """
    Returns true if a matrix does not contain NaNs, infs, or all 0s.
    """
    return not np.any(np.isinf(matrix)) and np.any(matrix)


def get_bond_angles(res, next_res):
    """
    Given 2 residues, returns the ncac, cacn, and cnca bond angles between
    them. If any atoms are not present, the corresponding angles are set to
    the GLOBAL_PAD_CHAR. If next_res is None, then only NCAC is measured.
    """
    # First residue angles
    n1, ca1, c1 = tuple(res.select(f"name {a}") for a in ["N", "CA", "C"])

    if n1 and ca1 and c1:
        ncac = safecalcAngle(n1, ca1, c1, radian=True)
    else:
        ncac = GLOBAL_PAD_CHAR

    # Second residue angles
    if next_res is None:
        return ncac, GLOBAL_PAD_CHAR, GLOBAL_PAD_CHAR
    n2, ca2 = (next_res.select(f"name {a}") for a in ["N", "CA"])
    if ca1 and c1 and n2:
        cacn = safecalcAngle(ca1, c1, n2, radian=True)
    else:
        cacn = GLOBAL_PAD_CHAR
    if c1 and n2 and ca2:
        cnca = safecalcAngle(c1, n2, ca2, radian=True)
    else:
        cnca = GLOBAL_PAD_CHAR
    return ncac, cacn, cnca


def safecalcAngle(a, b, c, radian):
    """
    Calculates the angle between 3 coordinates. If any of them are missing, the
    function raises a MissingAtomsError.
    """
    try:
        angle = pr.calcAngle(a, b, c, radian=radian)[0]
    except ValueError:
        raise MissingAtomsError
    return angle


def measure_bond_angles(residue, res_idx, all_res):
    """
    Given a residue, measure the ncac, cacn, and cnca bond angles.
    """
    if res_idx == len(all_res) - 1:
        next_res = None
    else:
        next_res = all_res[res_idx + 1]
    return list(get_bond_angles(residue, next_res))


def measure_phi_psi_omega(residue, include_OXT=False):
    """Measures a residue's primary backbone torsional angles (phi, psi, omega).

    Args:
        residue: ProDy residue object
        include_OXT: Boolean describing whether or not to measure an angle to
                     place the terminal oxygen.

    Returns:
        Python list of phi, psi, and omega angles for residue.

    """
    try:
        phi = pr.calcPhi(residue, radian=True)
    except ValueError:
        phi = GLOBAL_PAD_CHAR
    try:
        psi = pr.calcPsi(residue, radian=True)
    except ValueError:
        # For the last residue, we can measure a "psi" angle that is actually
        # the placement of the terminal oxygen. Currently, this is not utilized
        # in the building of structures, but it is included in case the need
        # arises in the future. Otherwise, this would simply become a pad
        # character.
        if include_OXT:
            try:
                psi = compute_single_dihedral(residue.select("name N CA C OXT"))
            except ValueError:
                psi = GLOBAL_PAD_CHAR
            except IndexError:
                psi = GLOBAL_PAD_CHAR
        else:
            psi = GLOBAL_PAD_CHAR
    try:
        omega = pr.calcOmega(residue, radian=True)
    except ValueError:
        omega = GLOBAL_PAD_CHAR
    return [phi, psi, omega]


def compute_single_dihedral(atoms):
    """
    Given an iterable of 4 Atoms, calculate the dihedral angle between them
    in radians.
    """
    if None in atoms:
        return GLOBAL_PAD_CHAR
    else:
        atoms = [a.getCoords()[0] for a in atoms]
        return get_dihedral(atoms[0], atoms[1], atoms[2], atoms[3], radian=True)


def get_dihedral(coords1, coords2, coords3, coords4, radian=False):
    """
    Returns the dihedral angle in degrees. Modified from
    prody.measure.measure to use a numerically safe normalization method.
    """
    rad2deg = 180 / np.pi
    eps = 1e-6

    a1 = coords2 - coords1
    a2 = coords3 - coords2
    a3 = coords4 - coords3

    v1 = np.cross(a1, a2)
    v1 = v1 / (v1 * v1).sum(-1)**0.5
    v2 = np.cross(a2, a3)
    v2 = v2 / (v2 * v2).sum(-1)**0.5
    porm = np.sign((v1 * a3).sum(-1))
    arccos_input_raw = (v1 * v2).sum(-1) / ((v1**2).sum(-1) * (v2**2).sum(-1))**0.5
    if -1 <= arccos_input_raw <= 1:
        arccos_input = arccos_input_raw
    elif arccos_input_raw > 1 and arccos_input_raw - 1 < eps:
        arccos_input = 1
    elif arccos_input_raw < -1 and np.abs(arccos_input_raw) - 1 < eps:
        arccos_input = -1
    else:
        raise ArithmeticError("Numerical issue with input to arccos.")
    rad = np.arccos(arccos_input)
    if not porm == 0:
        rad = rad * porm
    if radian:
        return rad
    else:
        return rad * rad2deg
