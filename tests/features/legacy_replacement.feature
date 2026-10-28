Feature: Legacy ambiguous replacement readiness

  Scenario: Standard Lite uses the account-specific non-Ultra per-generation rate
    Given one official Standard Lite NON_ULTRA cost row
    And the request is 8 seconds, 9:16, and one output
    When the replacement quote is calculated
    Then the quote is valid for 10 credits
    And the quote calculation creates no authorization, attempt, reservation, approval, or Generate

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
