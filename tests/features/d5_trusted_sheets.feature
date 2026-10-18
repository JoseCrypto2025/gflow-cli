Feature: Trusted D5 production master-data source

  Scenario: Refuse an operator snapshot at the production entry point
    Given the D5 process has no live Sheets credential
    When the operator passes a local snapshot path
    Then the production entry point refuses the snapshot argument

  Scenario: Reject a changed required Sheet header
    Given fake Sheets transport returns a changed header
    When the source assembles its snapshot
    Then the source returns a safe schema hold

  Scenario: Preserve independent TEST and PROD prompt approvals
    Given one approved TEST prompt and one approved PROD prompt for a Scene
    When the V2 validator checks the prompt rows
    Then both profile-scoped approvals are valid
