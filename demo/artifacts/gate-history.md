# Gate history -- what it did to real deploys

_Generated 2026-09-09 19:33 UTC by `scripts/gate_history.py`._

This is the REAL-WORLD rate, as distinct from the eval's fixture-based one.
A gate that scores well on 23 fixtures and halts half of your actual
deploys is not deployable, and only this number can tell you that.

```
==========================================================================
GATE HISTORY -- ai-pre-traffic-gate-demo-app
==========================================================================

  23 verdicts from real deploys, 2026-09-06 to 2026-09-09
  excluding 9 manual `aws lambda invoke` test(s) --
  no pipeline execution, so not a deploy. They fail closed to HIGH by
  design, so counting them would skew the rate upward.

  WOULD HAVE HALTED
    11/23  (48%)
    actually halted: 4

  RISK DISTRIBUTION
    low         4  #####
    medium      8  ##########
    high       11  ##############

  VERDICT SOURCE
    model            23

  CONTEXT
    modes:           {'enforcing': 7, 'advisory': 6, 'shadow': 10}
    prompt versions: {'2026-09-08.1': 7, '2026-09-02.3': 16}
    overridden:      2
    median latency:  3587ms
    input tokens:    65,473

  THE DECISIONS
    when              risk    source       mode      commit
    ----------------- ------- ------------ --------- ------------
  ! 2026-09-09T17:04  high    model        enforcing ccb5de72edac
  ! 2026-09-09T12:09  high    model        enforcing 11a8dba84a01
  ! 2026-09-09T12:06  high    model        enforcing 11a8dba84a01
  ! 2026-09-09T12:01  high    model        enforcing ce1d6eb564a4
  ! 2026-09-09T11:39  high    model        enforcing dbd492001602
  ! 2026-09-09T11:34  high    model        enforcing dbd492001602
  ! 2026-09-09T10:55  high    model        advisory  45a98e6d538d
    2026-09-08T17:05  medium  model        enforcing f3168b4b7e96
    2026-09-08T16:49  low     model        advisory  ac8054c615e3
  ! 2026-09-06T17:08  high    model        advisory  ecb6c0b60c77
  ! 2026-09-06T16:51  high    model        advisory  86ec99b19617
  ! 2026-09-06T16:42  high    model        advisory  d69afdeebe47
  ! 2026-09-06T16:33  high    model        advisory  87fad1acb356
    2026-09-06T16:14  medium  model        shadow    80824c08e81c
    2026-09-06T16:05  medium  model        shadow    6f0d98c680af
    2026-09-06T16:00  medium  model        shadow    e2ac352a6dbb
    2026-09-06T15:51  medium  model        shadow    d3f0441f66a6
    2026-09-06T15:40  medium  model        shadow    0448f3b1d754
    2026-09-06T15:34  medium  model        shadow    1c8c2916facf
    2026-09-06T14:52  low     model        shadow    d527df79dbfb

  READING THIS
    A halt rate this high will not survive contact with colleagues.
    Enforcing now means blocking real deploys often enough that
    somebody switches the gate off in week three.
```
