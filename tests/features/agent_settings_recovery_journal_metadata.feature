@e2e @e2e_auth
Feature: Agent settings recovery journals contain only restorable settings
  Scenario: Read-only production F5 ignores model display metadata
    Given the existing Run 3 production job and pinned Flow profile
    When the non-submitting F5 presubmit prepares the approved request
    Then the Agent settings journal is clean and the request is ready
    And no attempt, reservation, Generate action, or provider credit is created
