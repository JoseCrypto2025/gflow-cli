@e2e @e2e_auth
Feature: Read current Flow pricing without submitting a production generation

  Scenario: Inspect the existing gflow-prod project without changing Flow data
    Given the existing gflow-prod profile and exact production project
    When I run the read-only Flow pricing inspection
    Then the report verifies the expected Flow account and project
    And the report contains no provider mutation and no secret values
