Feature: Step records (turn-bound progress markers)
  As an agent driving the chat progress view
  I want to create step records distinct from regular tickets
  So the per-agent progress timeline stays scoped to my own turn-bound work

  Background:
    Given a clean tickets directory

  Scenario: Create a step record stamps step: true
    When I run "ticket create 'Read the middleware' --step"
    Then the command should succeed
    And the created ticket should have field "step" with value "true"

  Scenario: Create a regular ticket leaves the step field absent
    When I run "ticket create 'Regular ticket'"
    Then the command should succeed
    And the created ticket should contain "id:"
    And the created ticket should not contain "step:"

  Scenario: A step record id carries a -step- segment
    # The segment lets a consumer (the chat progress view) tell a step from a
    # regular ticket by the id alone -- needed when the ticket file is gone and
    # the step: true frontmatter is unreadable.
    When I run "ticket create 'Progress marker' --step"
    Then the command should succeed
    And the output should match pattern "-step-"

  Scenario: A regular ticket id has no -step- segment
    When I run "ticket create 'Regular ticket'"
    Then the command should succeed
    And the output should not contain "-step-"

  Scenario: ticket ready hides step records by default
    When I run "ticket create 'Plain ticket'"
    Then the command should succeed
    When I run "ticket create 'Progress marker' --step"
    Then the command should succeed
    When I run "ticket ready"
    Then the command should succeed
    And the output should contain "Plain ticket"
    And the output should not contain "Progress marker"

  Scenario: ticket ready --only-steps lists only step records
    When I run "ticket create 'Plain ticket'"
    Then the command should succeed
    When I run "ticket create 'Progress marker' --step"
    Then the command should succeed
    When I run "ticket ready --only-steps"
    Then the command should succeed
    And the output should contain "Progress marker"
    And the output should not contain "Plain ticket"

  Scenario: ticket ready --include-steps lists both
    When I run "ticket create 'Plain ticket'"
    Then the command should succeed
    When I run "ticket create 'Progress marker' --step"
    Then the command should succeed
    When I run "ticket ready --include-steps"
    Then the command should succeed
    And the output should contain "Plain ticket"
    And the output should contain "Progress marker"

  Scenario: Creating a step prints a Created decoration line carrying the title
    # The chat progress view reads the title straight from tk's stdout (the
    # transcript), so create echoes "Created <id>: <title>" for steps.
    When I run "ticket create 'Look through recent changes' --step"
    Then the command should succeed
    And the output should contain "Created "
    And the output should contain ": Look through recent changes"

  Scenario: Creating a regular ticket prints only the bare id
    When I run "ticket create 'Regular ticket'"
    Then the command should succeed
    And the output should not contain "Created "

  Scenario: Starting a step echoes a tk-step title line
    Given a step record exists with ID "tt-step-strt" and title "Trace the toggle"
    When I run "ticket start tt-step-strt"
    Then the command should succeed
    And the output should contain "Updated tt-step-strt -> in_progress"
    And the output should contain "tk-step tt-step-strt title: Trace the toggle"

  Scenario: Closing a step echoes tk-step title and summary lines
    Given a step record exists with ID "tt-step-clz" and title "Register the theme"
    When I run "ticket close tt-step-clz 'Wired the new theme into the toggle.'"
    Then the command should succeed
    And the output should contain "Updated tt-step-clz -> closed"
    And the output should contain "tk-step tt-step-clz title: Register the theme"
    And the output should contain "tk-step tt-step-clz summary: Wired the new theme into the toggle."

  Scenario: Starting a regular ticket emits no tk-step line
    Given a ticket exists with ID "tt-plain-strt" and title "Plain work"
    When I run "ticket start tt-plain-strt"
    Then the command should succeed
    And the output should not contain "tk-step"

  Scenario: tk close <id> "summary" writes the summary into a Summary section
    Given a ticket exists with ID "tt-summable" and title "Summable"
    When I run "ticket close tt-summable 'Did the thing.'"
    Then the command should succeed
    And ticket "tt-summable" should have field "status" with value "closed"
    And ticket "tt-summable" should contain "## Summary"
    And ticket "tt-summable" should contain "Did the thing."

  Scenario: tk show renders separate Children and Steps sections
    Given a ticket exists with ID "tt-parent" and title "Parent ticket"
    When I run "ticket create 'Child ticket' --parent tt-parent"
    Then the command should succeed
    When I run "ticket create 'Step under parent' --parent tt-parent --step"
    Then the command should succeed
    When I run "ticket show tt-parent"
    Then the command should succeed
    And the output should contain "## Children"
    And the output should contain "Child ticket"
    And the output should contain "## Steps"
    And the output should contain "Step under parent"

  Scenario: tk create as a mngr agent stamps the agent and leaves assignee unset
    When I run "ticket create 'Filed ticket'" as agent "agent-A"
    Then the command should succeed
    And the created ticket should have field "agent" with value "agent-A"
    And the created ticket should not contain "assignee:"

  Scenario: tk start as a mngr agent auto-self-assigns
    Given a ticket exists with ID "tt-pickup" and title "Pickup target"
    When I run "ticket start tt-pickup" as agent "agent-B"
    Then the command should succeed
    And ticket "tt-pickup" should have field "assignee" with value "agent-B"

  Scenario: tk start warns when reassigning across agents
    Given a ticket exists with ID "tt-conflict" and title "Already assigned"
    When I run "ticket assign tt-conflict old-agent"
    Then the command should succeed
    When I run "ticket start tt-conflict" as agent "new-agent"
    Then the command should succeed
    And the output should contain "reassigning"
    And ticket "tt-conflict" should have field "assignee" with value "new-agent"

  Scenario: tk assign sets the assignee field
    Given a ticket exists with ID "tt-assignable" and title "Assignable"
    When I run "ticket assign tt-assignable some-agent"
    Then the command should succeed
    And ticket "tt-assignable" should have field "assignee" with value "some-agent"

  Scenario: tk unassign clears the assignee value
    Given a ticket exists with ID "tt-unassignable" and title "Unassignable"
    When I run "ticket assign tt-unassignable temp-agent"
    Then the command should succeed
    When I run "ticket unassign tt-unassignable"
    Then the command should succeed
    And ticket "tt-unassignable" should not contain "temp-agent"
