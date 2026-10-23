@e2e @e2e_auth
Feature: gflow credits reads the balance flow.google.com shows
  Flow's migrated app loads its current balance in the app's own nzlxg response.

  Scenario: credits user reads the authenticated profile balance without generating
    Given a live profile that reaches the Flow app
    When I run "gflow credits user --json" for that profile
    Then the command exits 0
    And the JSON reports a non-negative integer balance for that profile
    And the balance equals the number Flow's account menu shows
