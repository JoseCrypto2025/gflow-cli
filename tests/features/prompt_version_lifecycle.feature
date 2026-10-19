Feature: Immutable prompt version approval
  Scenario: A stale candidate cannot supersede the approved prompt
    Given a current approved prompt and a stale validated candidate
    When prompt approval is attempted against fresh Phase E
    Then approval stops and the previous row remains unchanged

  Scenario: A fresh candidate replaces the current approval
    Given a persisted candidate that exactly matches fresh Phase E
    When the candidate is approved
    Then the prior approval is superseded and exactly one prompt remains approved
