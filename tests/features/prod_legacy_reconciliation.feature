Feature: Read-only reconciliation of legacy PROD attempts

  Scenario: Reconstruct a unique legacy identity without rewriting the attempt
    Given one legacy D2 attempt has unique canonical production joins
    When I inspect the legacy reconciliation evidence
    Then the identity is validated with provenance
    And the historical D2 row is unchanged

  Scenario: Missing provider evidence remains ambiguous and held for review
    Given one legacy D2 attempt has unique canonical production joins
    When I inspect the legacy reconciliation evidence
    Then the result remains AMBIGUOUS_AFTER_RECONCILIATION
    And the credit reservation remains unchanged
    And a premature RUNNING state is reported without a Sheet write

  Scenario: Duplicate canonical job linkage fails closed
    Given one legacy D2 attempt has duplicate Scene Job linkage
    When I inspect the legacy reconciliation evidence
    Then the identity is LEGACY_IDENTITY_AMBIGUOUS
    And the result remains AMBIGUOUS_AFTER_RECONCILIATION
