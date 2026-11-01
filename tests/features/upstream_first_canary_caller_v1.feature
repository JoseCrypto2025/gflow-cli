Feature: Upstream-first canary caller V1
  The canary caller binds fresh canonical evidence and a durable quote before invoking
  the upstream adapter once, then refuses to replay generation after an uncertain result.

  Scenario: Replaying an offline SPV_000007 canary reuses its reserved invocation
    Given an authorized SPV_000007 request with a fresh 10-credit quote
    When the offline upstream canary caller runs twice
    Then one durable invocation is retained with an exact 10-credit reservation
