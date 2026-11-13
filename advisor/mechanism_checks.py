"""Generic questions and authored coverage, never a physics acceptance test.

Profiles are selected explicitly by an author. Review expands profiles named by
known check IDs; it does not infer mechanisms from plan prose or select a route.
"""
from __future__ import annotations

from copy import deepcopy


_PROFILES = (
    ("conservation_coupling", "Conservation and coupling", "Exchange paths, measures, signs and balance accounting."),
    ("porous_transport", "Porous transport", "Averaged flow, transported quantities, storage and interfaces."),
    ("moving_interface", "Moving interfaces", "Interface motion, jump conditions, frames and state updates."),
    ("wave_transport", "Wave transport", "Wave regime, excitation, propagation, loss and feedback."),
    ("charge_transport", "Charge transport", "Field and carrier descriptions, currents and feedback."),
    ("radiative_transitions", "Radiative transitions", "Populations, spectral quantities, transport and energy transfer."),
)


def _check(identity, title, question, search_terms, needs):
    return {"id": identity, "title": title, "question": question,
            "search_terms": search_terms.split(" | "), "needs": needs.split()}


_CHECKS = (
    _check("common.variable_definitions", "Variable ownership and definitions",
           "Who defines and consumes each coupled variable, and what are its expression, units and valid scope?",
           "variable definition | dependent variable | coupling scope", "producer receiver scope expression unit"),
    _check("common.interface_constraints", "Interface constraints",
           "Which continuity, jump, exchange or other interface constraints are chosen, where do they act, and why are they applicable?",
           "interface condition | jump condition | constraint", "producer receiver scope expression unit activation"),
    _check("common.initialization_storage", "Initialization and stored states",
           "Which states are initialized, stored or reused, and how are their definitions and consistency established at each stage?",
           "initial condition | stored solution | consistent initialization", "producer receiver scope expression unit activation"),
    _check("common.mesh_distribution", "Mesh directions and domain distributions",
           "How do element directions and domain distributions resolve the selected mechanisms, and what comparison will assess adequacy?",
           "mesh direction | domain distribution | mesh convergence", "scope expression unit"),
    _check("common.solver_activation", "Solver activation and updates",
           "Which variables and couplings are active in each study stage, and when are dependent coefficients and transferred fields updated?",
           "study activation | solver sequence | coupling update", "producer receiver scope activation"),
    _check("common.output_definitions", "Output definitions and checks",
           "What expressions, units, locations and evaluation operations define the requested outputs, and how will they be checked?",
           "derived value | evaluation scope | verification", "producer scope expression unit activation"),
    _check("conservation_coupling.exchange_path", "Exchange producers and receivers",
           "For each retained exchange, which field produces it, which equation receives it, and what scope and expression connect them?",
           "multiphysics source | coupling variable | equation contribution", "producer receiver scope expression unit activation"),
    _check("conservation_coupling.measure_conversion", "Domain and boundary measures",
           "How are contributions converted between any volume, surface, line or lumped measures used by the selected description?",
           "surface source | volumetric source | geometric measure", "producer receiver scope expression unit"),
    _check("conservation_coupling.sign_convention", "Signs and orientations",
           "What sign, normal and reference conventions connect the exchanged quantities, including contributions on either side of an interface?",
           "flux sign | boundary normal | exchange convention", "producer receiver scope expression unit"),
    _check("conservation_coupling.state_flux_relation", "States, fluxes and rates",
           "Which exchanges are states, fluxes or rates, and how are their chosen definitions connected without duplicating a contribution?",
           "state variable | flux definition | source accounting", "producer receiver scope expression unit activation"),
    _check("conservation_coupling.property_updates", "State dependent properties",
           "Which properties depend on solved or transferred states, and when are those dependencies evaluated or intentionally held fixed?",
           "state dependent coefficient | material update | field dependency", "producer receiver scope expression unit activation"),
    _check("conservation_coupling.balance_closure", "Balance accounting",
           "Which storage, transport, production and loss terms enter the selected balance, and what residual or independent comparison will assess closure?",
           "conservation balance | integrated flux | balance residual", "producer receiver scope expression unit activation"),
    _check("porous_transport.momentum_description", "Flow description",
           "Which porous flow or prescribed velocity description is chosen, what connects pressure and velocity, and why is that choice valid?",
           "porous flow | averaged velocity | pressure relation", "producer receiver scope expression unit activation"),
    _check("porous_transport.thermal_advection", "Thermal advection",
           "Which velocity and averaging convention supplies energy transport, how does it enter the thermal equation, or why is it omitted?",
           "porous heat advection | velocity convention | convective heat flux", "producer receiver scope expression unit activation"),
    _check("porous_transport.thermal_storage", "Thermal storage and phases",
           "Which phases store energy, how are their temperatures and effective capacities defined, and why is the selected thermal description applicable?",
           "porous heat capacity | phase temperature | energy storage", "producer receiver scope expression unit activation"),
    _check("porous_transport.species_transport", "Species transport conventions",
           "For retained species, which concentration and velocity measures define advection, diffusion, dispersion and reactions, or why are they absent?",
           "porous species transport | concentration convention | dispersion", "producer receiver scope expression unit activation"),
    _check("porous_transport.constitutive_coefficients", "Effective transport coefficients",
           "How are porosity and any directional or state dependent transport coefficients defined, distributed and updated?",
           "effective coefficient | porous anisotropy | porosity dependence", "producer receiver scope expression unit activation"),
    _check("porous_transport.interface_exchange", "Porous and adjacent region exchange",
           "What flow, thermal or species quantities cross each porous interface, and how do definitions and constraints match adjacent regions?",
           "porous interface | flux continuity | interfacial exchange", "producer receiver scope expression unit activation"),
    _check("porous_transport.domain_distribution", "Porous domain distributions",
           "Where are porous subregions and spatially varying properties assigned, and how do mesh distributions resolve transport directions and gradients?",
           "porous domain selection | directional mesh | spatial coefficient", "scope expression unit activation"),
    _check("moving_interface.interface_kinematics", "Interface kinematics",
           "What determines interface position and velocity, which field supplies that relation, and why is the selected motion law applicable?",
           "interface velocity | moving boundary | kinematic condition", "producer receiver scope expression unit activation"),
    _check("moving_interface.mass_energy_jump", "Interface balance and jumps",
           "Which mass, energy or other jumps accompany motion, and how are their signs, phase measures and coupling terms defined?",
           "moving interface balance | phase flux | jump relation", "producer receiver scope expression unit activation"),
    _check("moving_interface.motion_representation", "Motion representation",
           "Which representation tracks the evolving interface, what alternatives were considered, and what assumptions constrain the choice?",
           "interface tracking | geometry motion | representation assumptions", "producer receiver scope expression unit activation"),
    _check("moving_interface.reference_frames", "Transport reference frames",
           "Which frames define material, fluid, mesh and interface velocities, and how are relative transport terms formed where needed?",
           "relative velocity | reference frame | moving mesh transport", "producer receiver scope expression unit activation"),
    _check("moving_interface.initial_interface_state", "Initial interface and history",
           "How are initial interface location, motion state and any transferred or accumulated states established consistently?",
           "initial interface | accumulated state | motion initialization", "producer receiver scope expression unit activation"),
    _check("moving_interface.mesh_deformation_distribution", "Mesh response to motion",
           "Which domains and boundaries move, remain constrained or change mesh, and how are directional resolution and mesh quality assessed throughout motion?",
           "mesh displacement scope | mesh constraint | moving mesh quality", "producer receiver scope expression unit activation"),
    _check("moving_interface.update_sequence", "Motion and field update sequence",
           "When are interface motion, field solutions, coefficients and transferred states updated, and what consistency check supports the chosen sequence?",
           "interface update | coupled time stepping | state transfer", "producer receiver scope expression unit activation"),
    _check("wave_transport.governing_regime", "Wave regime and assumptions",
           "Which wave description and dependent variables are selected, and what scales and assumptions justify that description and its alternatives?",
           "wave equation | propagation regime | field approximation", "producer scope expression unit activation"),
    _check("wave_transport.source_excitation", "Excitation and normalization",
           "How are excitation, polarization or modal content and amplitude or power normalization defined for the selected wave description?",
           "wave excitation | polarization | power normalization", "producer receiver scope expression unit activation"),
    _check("wave_transport.boundary_interfaces", "Wave boundaries and interfaces",
           "Which boundary and interface conditions are chosen, where do they act, and how is their consistency with the selected propagation problem checked?",
           "wave boundary | interface continuity | reflection", "producer receiver scope expression unit activation"),
    _check("wave_transport.material_feedback", "Material response and feedback",
           "Which constitutive response connects the wave field to material states, and which state changes update that response?",
           "wave constitutive relation | state dependent response | feedback", "producer receiver scope expression unit activation"),
    _check("wave_transport.loss_feedback", "Wave loss and receiver equations",
           "Which loss or transfer expressions feed other equations, with what averaging and units, or why is that transfer omitted?",
           "wave loss density | time averaging | energy transfer", "producer receiver scope expression unit activation"),
    _check("wave_transport.spectral_resolution", "Spatial and spectral resolution",
           "Which spatial, temporal or spectral scales require resolution, and how are mesh directions, sampling and solver stages chosen and checked?",
           "wavelength resolution | frequency sampling | wave mesh", "scope expression unit activation"),
    _check("charge_transport.field_description", "Field description",
           "Which electric or coupled field description is selected, and what scales, material assumptions and omitted terms justify the choice?",
           "electric field formulation | charge conservation | field regime", "producer receiver scope expression unit activation"),
    _check("charge_transport.carrier_states", "Carrier state definitions",
           "Which carrier or charge states are retained, what are their units and signs, and how do their source and loss terms connect?",
           "carrier density | charge state | charge source", "producer receiver scope expression unit activation"),
    _check("charge_transport.flux_constitutive", "Current and transport relations",
           "How do field, state and coefficient definitions produce current or carrier flux, including directional or state dependent effects where retained?",
           "current density | carrier flux | transport coefficient", "producer receiver scope expression unit activation"),
    _check("charge_transport.boundary_interface_current", "Boundary and interface currents",
           "What potential, charge or current conditions act at boundaries and interfaces, and how are exchange signs and constraints defined?",
           "electric boundary condition | interface current | charge flux", "producer receiver scope expression unit activation"),
    _check("charge_transport.initial_storage", "Charge initialization and storage",
           "Which charge or field states store history, how are they initialized, and when are their equations active or reused?",
           "charge storage | field initialization | transient current", "producer receiver scope expression unit activation"),
    _check("charge_transport.heating_feedback", "Electrical energy and state feedback",
           "Which electrical work or loss expressions feed energy or other equations, and how do resulting state changes update electrical properties?",
           "electrical heating | power density | conductivity feedback", "producer receiver scope expression unit activation"),
    _check("radiative_transitions.population_states", "Population state definitions",
           "Which transition populations are retained, how are their densities or fractions defined and initialized, and what constrains their evolution?",
           "population balance | state fraction | transition rate", "producer receiver scope expression unit activation"),
    _check("radiative_transitions.spectral_variables", "Spectral quantity definitions",
           "Which spectral coordinate and dependent radiative quantities are chosen, and what units, bandwidth measures and transformations define them?",
           "spectral variable | spectral density | bandwidth measure", "producer receiver scope expression unit"),
    _check("radiative_transitions.spectral_energy_mapping", "Spectral to energy mapping",
           "How are spectral or population quantities converted into exchanged energy or power, including coordinate measures and integration weights?",
           "photon energy mapping | spectral integration | energy transfer", "producer receiver scope expression unit activation"),
    _check("radiative_transitions.emission_absorption", "Emission and absorption contributions",
           "Which emission, absorption and other retained transition contributions enter population and radiation equations, with what signs and scopes?",
           "emission source | absorption rate | transition coupling", "producer receiver scope expression unit activation"),
    _check("radiative_transitions.angular_transport", "Radiative transport and constraints",
           "Which angular and spatial transport description and boundary constraints are chosen, and how is their resolution or approximation justified?",
           "angular radiation transport | radiation boundary | transport approximation", "producer receiver scope expression unit activation"),
    _check("radiative_transitions.energy_population_feedback", "Energy and population feedback",
           "How do radiation, populations and material energy feed each other, and when are those exchanges and state dependent rates updated?",
           "radiative energy balance | population feedback | rate update", "producer receiver scope expression unit activation"),
)

CHECK_IDS = frozenset(item["id"] for item in _CHECKS)
_PROFILE_IDS = tuple(item[0] for item in _PROFILES)
_STATUS_ORDER = ("adopted", "adapted", "alternative", "unresolved", "not_applicable", "unspecified")
_ACTIVE_STATUSES = frozenset({"adopted", "adapted", "alternative"})
_MAX_ROWS = 64
_MAX_CHECK_IDS_PER_ROW = 16


def known_check_ids():
    """Return the immutable set of valid atomic check identities."""
    return CHECK_IDS


def known_mechanisms():
    """The same profile identifiers accepted by the runtime checklist."""
    return _PROFILE_IDS


def mechanism_checklist(mechanisms=None):
    """Return common questions plus selected profiles, without selecting a route.

    ``None`` and ``[]`` expose the selectable catalog and only common checks.
    The result is detached from module data so callers may edit it safely.
    """
    if mechanisms is None:
        mechanisms = []
    if (not isinstance(mechanisms, list) or len(mechanisms) > len(_PROFILE_IDS)
            or any(not isinstance(item, str) or item not in _PROFILE_IDS for item in mechanisms)):
        raise ValueError("mechanisms must be a list of at most six known profile IDs")
    selected = [identity for identity in _PROFILE_IDS if identity in mechanisms]
    wanted = {"common", *selected}
    catalog = [{"id": identity, "title": title, "description": description,
                "check_count": sum(item["id"].startswith(identity + ".") for item in _CHECKS)}
               for identity, title, description in _PROFILES]
    return {"schema": "mechanism-checklist/v1", "status": "advisory",
            "physical_correctness": "not_evaluated", "catalog": catalog,
            "selected_profiles": selected,
            "checks": deepcopy([item for item in _CHECKS if item["id"].split(".", 1)[0] in wanted])}


def review_mechanism_coverage(rows, plan=None):
    """Review explicit check IDs and dispositions, never free-text physics.

    All checks in every named profile and the common group are expected. A
    mapping records an author's disposition, including unresolved or inapplicable
    items. It is not verification that a mechanism is implemented correctly.
    ``plan`` is optional and only supplies known implementation step identities.
    """
    if rows is None:
        rows = []
    if not isinstance(rows, list) or len(rows) > _MAX_ROWS:
        raise ValueError("rows must be an array of at most 64 authored mappings")
    if plan is not None and not isinstance(plan, dict):
        raise ValueError("plan must be an object when supplied")
    mappings, unknown, invalid_status_rows = {}, set(), []
    missing_implementation_rows, unknown_step_rows = [], []
    raw_steps = plan.get("steps", []) if plan is not None else []
    step_ids = {item["id"] for item in raw_steps[:64]
                if isinstance(item, dict) and isinstance(item.get("id"), str)} if isinstance(raw_steps, list) else set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("each mechanism mapping must be an object")
        identities = row.get("check_ids", [])
        if (not isinstance(identities, list) or len(identities) > _MAX_CHECK_IDS_PER_ROW
                or any(not isinstance(identity, str) or not identity.strip() or len(identity) > 120 for identity in identities)):
            raise ValueError("check_ids must contain at most 16 bounded nonempty IDs")
        known = sorted(set(identities) & CHECK_IDS)
        unknown.update(set(identities) - CHECK_IDS)
        if not known:
            continue
        status = row.get("status", "unspecified")
        if not isinstance(status, str) or status not in _STATUS_ORDER[:-1]:
            status = "unspecified"
            invalid_status_rows.append(index)
        if status in _ACTIVE_STATUSES:
            implementation = row.get("implementation")
            explicit = (isinstance(implementation, dict)
                        and isinstance(implementation.get("step_id"), str)
                        and bool(implementation["step_id"].strip())
                        and implementation.get("field") in ("action", "inputs", "checks"))
            if not explicit:
                missing_implementation_rows.append(index)
            elif plan is not None and implementation["step_id"] not in step_ids:
                unknown_step_rows.append(index)
        for identity in known:
            mappings.setdefault(identity, []).append((index, status))
    selected = [identity for identity in _PROFILE_IDS
                if any(check_id.startswith(identity + ".") for check_id in mappings)]
    expected = [item["id"] for item in mechanism_checklist(selected)["checks"]]
    mapped = [identity for identity in expected if identity in mappings]
    unaddressed = [identity for identity in expected if identity not in mappings]
    dispositions = []
    for identity in expected:
        entries = mappings.get(identity, [])
        statuses = [status for status in _STATUS_ORDER if any(entry[1] == status for entry in entries)]
        disposition = (statuses[0] if len(statuses) == 1 else "mixed_authored_status") if entries else "unaddressed"
        dispositions.append({"check_id": identity, "status": disposition,
                             "authored_statuses": statuses, "row_indexes": [entry[0] for entry in entries]})
    counts = {"expected": len(expected), "mapped": len(mapped), "unaddressed": len(unaddressed),
              "active": sum(any(status in _ACTIVE_STATUSES for _, status in entries)
                            for entries in mappings.values()),
              **{status: sum(any(entry[1] == status for entry in entries) for entries in mappings.values())
                 for status in _STATUS_ORDER},
              "mixed_authored_status": sum(item["status"] == "mixed_authored_status" for item in dispositions),
              "unknown_check_ids": len(unknown)}
    warnings = []

    def warn(code, message, **where):
        warnings.append({"code": code, "category": "implementation_check", "message": message, **where})

    if not mappings:
        warn("mechanism_coverage_unknown", "No known check IDs were authored; mechanism coverage is unknown.")
    if not selected:
        warn("mechanism_profile_scope_unknown", "No mechanism profile is identified; common questions cannot establish mechanism scope.")
    if unknown:
        warn("mechanism_check_unknown", "Unknown check IDs do not establish coverage.",
             check_ids=sorted(unknown)[:16], count=len(unknown), truncated=len(unknown) > 16)
    if unaddressed:
        warn("mechanism_check_unaddressed", "These expected questions have no authored disposition; review them for the selected route.", check_ids=unaddressed)
    for status, code, message in (
        ("unresolved", "mechanism_check_unresolved", "Unresolved dispositions remain open author questions."),
        ("not_applicable", "mechanism_check_not_applicable", "Not-applicable dispositions require author justification; applicability was not evaluated."),
    ):
        identities = [identity for identity in mapped if any(entry[1] == status for entry in mappings[identity])]
        if identities:
            warn(code, message, check_ids=identities)
    if invalid_status_rows:
        warn("mechanism_disposition_unspecified", "A recognized check ID lacks a known authored disposition.", row_indexes=invalid_status_rows)
    if missing_implementation_rows:
        warn("mechanism_implementation_missing", "Active authored dispositions need a concrete implementation field reference.", row_indexes=missing_implementation_rows)
    if unknown_step_rows:
        warn("mechanism_implementation_step_unknown", "Active authored references identify steps absent from the supplied plan.", row_indexes=unknown_step_rows)
    return {"schema": "mechanism-coverage/v1", "status": "advisory", "blocking": False,
            "physical_correctness": "not_evaluated", "source_applicability": "agent_review_required",
            "coverage_state": "unknown" if not mappings else "unaddressed_checks" if unaddressed else "authored_dispositions",
            "coverage_scope": "profiles_inferred_from_explicit_check_ids_plus_common",
            "selected_profiles": selected, "expected_check_ids": expected, "mapped_check_ids": mapped,
            "unaddressed_check_ids": unaddressed, "dispositions": dispositions, "counts": counts,
            "warnings": warnings,
            "mapping_meaning": "Mapped and active counts record authored claims only; they do not verify physical completeness or correctness. Disposition counts may overlap for mixed authored statuses."}
