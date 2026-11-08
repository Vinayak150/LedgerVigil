"""Installed simulation versions. A bundle records one installed profile at capture; replay refuses if the pinned profile is not
installed exactly (Stage 2B Family 9: "409 if a pinned rule version is no longer installed"; Stage 2A §10.2).

Four simulator profiles are installed:

* `lv-native-recordsettlement/0.1.0` (the simulation foundation): `policy_set` None, outcome format 1. Runs recorded under it
  keep `verification: NOT_EVALUATED` forever. They replay through the unchanged legacy path; verdicts are never retrofitted.
* `lv-native-recordsettlement/0.2.0` (previous verified profile): outcome format 2, `policy_set` `sp1-policies/1.1`
  and `policy_registry` = policy set + SHA-256 of the registry source (ADR-014: semantic version plus content hash). Any
  change to any policy rule changes the identifier, so a run pinned to another registry refuses to replay (409).

`lv-native-recordsettlement/0.2.1` pins the complete Decimal context.
`lv-native-recordsettlement/0.3.0` is the capture default and adds account configuration/balance versions to its captured read set.

The kernel's settlement-slice check version stays recorded: its diagnostics remain labelled diagnostics, not verdicts.
"""
import ledger_kernel
from ledger_kernel.settlement import CHECK_VERSION, RULE_VERSION

from ledgervigil.kernel_adapter.calculation import TYPED_RULE
from ledgervigil.kernel_adapter.types import CONTRACT

from . import policies

LEGACY_SIMULATOR = 'lv-native-recordsettlement/0.1.0'
PREVIOUS_SIMULATOR = 'lv-native-recordsettlement/0.2.0'
DECIMAL_SIMULATOR = 'lv-native-recordsettlement/0.2.1'
SIMULATOR = 'lv-native-recordsettlement/0.3.0'
BUNDLE_FORMAT = 'lv-simulation-input/1'
LEGACY_OUTCOME_FORMAT = 'lv-simulation-outcome/1'
OUTCOME_FORMAT = 'lv-simulation-outcome/2'
PROFILE = 'sp1/1.0'

_COMMON = {'bundle_format': BUNDLE_FORMAT, 'profile': PROFILE, 'posting_rule': TYPED_RULE,
           'kernel_package': f'ledger_kernel/{ledger_kernel.__version__}', 'kernel_rule': RULE_VERSION, 'kernel_checks': CHECK_VERSION,
           'adapter': CONTRACT}
PROFILES = {
    LEGACY_SIMULATOR: {**_COMMON, 'simulator': LEGACY_SIMULATOR, 'outcome_format': LEGACY_OUTCOME_FORMAT, 'policy_set': None},
    SIMULATOR: {**_COMMON, 'simulator': SIMULATOR, 'outcome_format': OUTCOME_FORMAT, 'policy_set': policies.POLICY_SET,
                'policy_registry': policies.REGISTRY_ID},
}

# Preserve Claude's frozen 0.2.0 runs. Rules and output format are unchanged; 0.2.1 explicitly
# pins the Decimal context around the complete calculation. Old-profile equivalence is checked offline.
PROFILES[DECIMAL_SIMULATOR] = {**PROFILES[SIMULATOR], 'simulator': DECIMAL_SIMULATOR}
PROFILES[PREVIOUS_SIMULATOR] = {**PROFILES[SIMULATOR], 'simulator': PREVIOUS_SIMULATOR}

PROFILES[SIMULATOR]['account_read_set'] = 'lv-account-dependencies/1'


def installed(simulator: str | None = None) -> dict:
    """The installed version set for `simulator` (default: SIMULATOR, the profile new captures pin)."""
    return dict(PROFILES[simulator or SIMULATOR])
