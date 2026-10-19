@e2e @e2e_auth
Feature: F5 maps harmless Flow model label punctuation to the configured model

  Scenario: The production Agent composer presents the configured model with a separator
    Given the isolated gflow production profile and pinned Flow project
    When the repository-owned Agent port reads the live baseline
    Then the session project and Agent-only surface are verified
    And the displayed Veo label resolves uniquely to MDL_0001
