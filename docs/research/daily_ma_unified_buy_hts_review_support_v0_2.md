# Daily MA Unified BUY V0.2 — HTS Review Support

Python V0.2 is the canonical research implementation.  Kiwoom HTS Formula
Manager / Signal Search may be used only to reproduce a signal manually after
looking it up in the V0.2 registry; it is not an execution or validation
authority.

The independently inspectable components are:

- adjusted-Daily MA5, MA10, MA20, MA60, and MA120;
- MA10/MA20 `+2%` cross-up and MA10 `-2%` cross-down, each with the V0.2
  previous-session boundary condition;
- consecutive `Close < MA10` and `Close < MA20` counts;
- the structural MA inflection: the most recent tied low inside a trailing
  MA-value window, followed by the current MA exceeding it with no intervening
  MA above the current value;
- BUY-A to BUY-B lineage, including the three-session failure window and
  ten-session reclaim window.

HTS formula syntax, adjustment conventions, and available state/lineage
variables may differ from Python.  Therefore manual review should compare the
stock code, date, and causal markers in the registry/chart rather than assume
formula equivalence.  No HTS automation is implemented by this project.
