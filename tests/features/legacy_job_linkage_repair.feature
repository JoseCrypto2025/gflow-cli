Feature: Repair missing legacy D2 job linkage

  Scenario: A unique canonical join backfills only linkage and audit
    Given a legacy attempt with a unique canonical linkage proof
    When the proof-bound repair is applied
    Then only the two parent job IDs are added with a durable audit
    And the reservation, state, Generate count, and provider evidence stay unchanged

  Scenario: The exact repair can be replayed idempotently
    Given a legacy attempt with a unique canonical linkage proof
    When the proof-bound repair is applied
    And the same proof-bound repair is replayed
    Then the original linkage audit is returned without a duplicate event

  Scenario: Ambiguous canonical candidates do not mutate the ledger
    Given a legacy attempt with duplicate canonical scene-job candidates
    When the proof-bound repair is attempted
    Then the proof is rejected and the ledger remains unchanged
