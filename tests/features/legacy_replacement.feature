Feature: Legacy ambiguous replacement readiness

  Scenario: Missing authoritative cost keeps the replacement decision on hold
    Given a quarantined ambiguous legacy attempt with a 50-credit unresolved reservation
    And no applicable verified credit-price row
    When the replacement decision is evaluated
    Then the quote is unavailable and the decision is HOLD
    And no authorization, replacement attempt, or reservation is created

  Scenario: Consuming an approved authorization links one replacement attempt
    Given an approved authorization bound to a quarantined legacy parent
    When a controlled replacement creation consumes the authorization
    Then the replacement records its parent and authorization IDs
    And the authorization is consumed exactly once
    And a second or different consumption fails
