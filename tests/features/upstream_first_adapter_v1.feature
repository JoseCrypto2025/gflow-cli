Feature: Offline upstream video adapter

  Scenario: Unauthorized generation never invokes the upstream CLI
    Given an unauthorized upstream video request
    When the adapter generates once
    Then the adapter reports a pre-submit failure
    And the upstream command runner was not called

  Scenario: A timeout after process launch becomes an unknown non-retryable result
    Given an authorized upstream video request
    And the upstream command runner times out after launch
    When the adapter generates once
    Then the adapter reports post-submit unknown
    And automatic retry is disabled

  Scenario: Upstream success is not mistaken for production finalization
    Given an authorized upstream video request
    And the upstream command runner returns a completed staged MP4
    When the adapter generates once
    Then the adapter reports upstream generation completed
    And the result does not claim production finalization

  Scenario: Submit evidence is preserved before malformed output is classified
    Given an authorized upstream video request
    And the upstream command runner emits submission evidence then malformed output
    When the adapter generates once
    Then the adapter reports post-submit unknown
    And submission evidence is persisted before the parse diagnostic
    And the upstream video command was invoked once
