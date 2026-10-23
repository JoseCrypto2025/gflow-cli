Feature: bounded TEST D2 session credit authorization

  Scenario: Preserve the historical reservation while starting an authorized TEST session
    Given a ledger with a preserved historical 50-credit reservation
    And an exact TEST authorization capped at 40 new credits
    When one authorized TEST attempt is reserved
    Then the historical row is unchanged
    And the attempt is bound to the exact TEST account and project

  Scenario: Stop the session when the provider outcome is unknown
    Given an authorized TEST session with remaining credit allowance
    When an attempt has an unknown provider outcome
    Then a second attempt is rejected before Generate

  Scenario: Reject concurrent attempts in one TEST session
    Given one authorized TEST session
    When two distinct attempts are reserved concurrently
    Then exactly one attempt is in flight
