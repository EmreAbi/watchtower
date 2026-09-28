use super::*;

fn group_state() -> ClientShellState {
    let mut state = ClientShellState::new(ClientShellConfig::from_config(&Config::default()));
    state.set_snapshot(Box::new(snapshot()));
    state.set_pane_surface(surface());
    state
}

fn activate(state: &mut ClientShellState, action: ClientContextMenuAction) -> ClientShellInput {
    let Some(ClientShellOverlay::ContextMenu(menu)) = state.overlay.as_ref() else {
        panic!("expected context menu");
    };
    let index = menu
        .items()
        .iter()
        .position(|item| item.action == action)
        .expect("menu action");
    let mut outcome = ClientShellInput::default();
    state.activate_context_menu_item(index, &mut outcome);
    outcome
}

fn assert_local(outcome: &ClientShellInput) {
    assert!(
        outcome.actions.is_empty(),
        "grouping must not issue endpoint actions"
    );
    assert!(
        outcome.requests.is_empty(),
        "grouping must not issue runtime requests"
    );
}

fn name_overlay(state: &mut ClientShellState, name: &str) -> ClientShellInput {
    let Some(ClientShellOverlay::Rename(rename)) = state.overlay.as_mut() else {
        panic!("group name input");
    };
    rename.input = TextEditor::new(name, false);
    let mut outcome = ClientShellInput::default();
    state.save_rename_overlay(&mut outcome);
    outcome
}

#[test]
fn spaces_dropdown_uses_mouse_and_local_preferences() {
    let mut state = group_state();
    state.compose(106, 30).unwrap();
    let toggle = state.hits.spaces_grouping_toggle;
    assert!(toggle.width > 0);
    let open = state.handle_raw_events(vec![RawInputEvent::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: toggle.x,
        row: toggle.y,
        modifiers: KeyModifiers::empty(),
    })]);
    assert_local(&open);
    let grouped = activate(&mut state, ClientContextMenuAction::SetSpacesGrouped(true));
    assert_local(&grouped);
    assert!(state.config.preferences.space_groups.grouped);
    state.open_spaces_grouping_menu(1, 1);
    let flat = activate(&mut state, ClientContextMenuAction::SetSpacesGrouped(false));
    assert_local(&flat);
    assert!(!state.config.preferences.space_groups.grouped);
}

#[test]
fn empty_workspace_list_can_create_rename_reorder_and_remove_groups_without_closing() {
    let mut state = group_state();
    let mut empty = snapshot();
    empty.workspaces.clear();
    empty.tabs.clear();
    empty.panes.clear();
    empty.focused_workspace_id = None;
    empty.focused_tab_id = None;
    empty.focused_pane_id = None;
    state.set_snapshot(Box::new(empty));
    state.open_spaces_grouping_menu(1, 1);
    assert_local(&activate(
        &mut state,
        ClientContextMenuAction::NewSpaceGroup,
    ));
    assert_local(&name_overlay(&mut state, "Research"));
    let first = state.config.preferences.space_groups.groups[0].id.clone();
    state.open_new_space_group_overlay(ClientEndpointId::Local, None);
    assert_local(&name_overlay(&mut state, "Development"));
    let second = state.config.preferences.space_groups.groups[1].id.clone();
    state.open_space_group_context_menu(ClientEndpointId::Local, second.clone(), 1, 1);
    assert_local(&activate(
        &mut state,
        ClientContextMenuAction::MoveSpaceGroup(-1),
    ));
    assert_eq!(state.config.preferences.space_groups.groups[0].id, second);
    state.open_space_group_context_menu(ClientEndpointId::Local, first.clone(), 1, 1);
    assert_local(&activate(
        &mut state,
        ClientContextMenuAction::RenameSpaceGroup,
    ));
    assert_local(&name_overlay(&mut state, "Research notes"));
    assert_eq!(
        state.config.preferences.space_groups.groups[1].name,
        "Research notes"
    );
    state.open_space_group_context_menu(ClientEndpointId::Local, first, 1, 1);
    assert_local(&activate(
        &mut state,
        ClientContextMenuAction::RemoveSpaceGroup,
    ));
    assert_eq!(state.config.preferences.space_groups.groups.len(), 1);
    assert!(state.snapshot.as_ref().unwrap().workspaces.is_empty());
}

#[test]
fn moving_workspace_assigns_the_entire_worktree_family_and_remove_preserves_runtime() {
    let mut state = group_state();
    let mut family = snapshot();
    family.workspaces[0].worktree = Some(ClientShellWorktree {
        key: "repo".into(),
        label: "repo".into(),
        is_linked_worktree: false,
    });
    let mut child = family.workspaces[0].clone();
    child.workspace_id = "ws_child".into();
    child.focused = false;
    child.worktree.as_mut().unwrap().is_linked_worktree = true;
    family.workspaces.push(child);
    let original = family.workspaces.clone();
    state.set_snapshot(Box::new(family));
    let id = state
        .config
        .preferences
        .space_groups
        .create("local", "Work")
        .unwrap();
    state.open_workspace_context_menu("ws_child".into(), 1, 1);
    assert_local(&activate(
        &mut state,
        ClientContextMenuAction::MoveToSpaceGroup,
    ));
    assert_local(&activate(
        &mut state,
        ClientContextMenuAction::AssignSpaceGroup(Some(0)),
    ));
    let members = &state.config.preferences.space_groups.groups[0].workspace_ids;
    assert!(members.contains(&"ws_1".to_owned()));
    assert!(members.contains(&"ws_child".to_owned()));
    state.open_space_group_context_menu(ClientEndpointId::Local, id, 1, 1);
    assert_local(&activate(
        &mut state,
        ClientContextMenuAction::RemoveSpaceGroup,
    ));
    assert!(state.config.preferences.space_groups.groups.is_empty());
    assert_eq!(state.snapshot.as_ref().unwrap().workspaces, original);
}

#[test]
fn stale_workspace_or_group_does_not_reassign_or_create_an_empty_group() {
    let mut state = group_state();
    let id = state
        .config
        .preferences
        .space_groups
        .create("local", "Work")
        .unwrap();
    state.open_workspace_context_menu("ws_1".into(), 1, 1);
    activate(&mut state, ClientContextMenuAction::MoveToSpaceGroup);
    state
        .config
        .preferences
        .space_groups
        .remove("local", &id)
        .unwrap();
    assert_local(&activate(
        &mut state,
        ClientContextMenuAction::AssignSpaceGroup(Some(0)),
    ));
    assert!(state.config.preferences.space_groups.groups.is_empty());
    assert!(state.visible_endpoint_notice.is_some());

    state.open_workspace_context_menu("ws_1".into(), 1, 1);
    activate(&mut state, ClientContextMenuAction::MoveToSpaceGroup);
    activate(&mut state, ClientContextMenuAction::NewSpaceGroup);
    state.active_snapshot_generation = Some(42);
    assert_local(&name_overlay(&mut state, "Do not create"));
    assert!(state.config.preferences.space_groups.groups.is_empty());
}

#[test]
fn group_context_captures_endpoint_and_workspace_move_rejects_endpoint_switch() {
    let mut state = group_state();
    let profile = SavedSshEndpoint::new("Remote", "dev@example.invalid", "fixture").unwrap();
    let remote = ClientEndpointId::Ssh(profile.id.clone());
    state.set_endpoint_catalog(&[profile]);
    let local_id = state
        .config
        .preferences
        .space_groups
        .create("local", "Same name")
        .unwrap();
    let remote_id = state
        .config
        .preferences
        .space_groups
        .create(&remote.storage_key(), "Same name")
        .unwrap();
    state.open_space_group_context_menu(ClientEndpointId::Local, local_id.clone(), 1, 1);
    activate(&mut state, ClientContextMenuAction::RenameSpaceGroup);
    state.active_endpoint_id = remote.clone();
    assert_local(&name_overlay(&mut state, "Local renamed"));
    assert_eq!(
        state
            .config
            .preferences
            .space_groups
            .groups
            .iter()
            .find(|g| g.id == local_id)
            .unwrap()
            .name,
        "Local renamed"
    );
    assert_eq!(
        state
            .config
            .preferences
            .space_groups
            .groups
            .iter()
            .find(|g| g.id == remote_id)
            .unwrap()
            .name,
        "Same name"
    );
    state.active_endpoint_id = ClientEndpointId::Local;
    state.open_workspace_context_menu("ws_1".into(), 1, 1);
    activate(&mut state, ClientContextMenuAction::MoveToSpaceGroup);
    state.active_endpoint_id = remote;
    assert_local(&activate(
        &mut state,
        ClientContextMenuAction::AssignSpaceGroup(Some(0)),
    ));
    assert!(state
        .config
        .preferences
        .space_groups
        .groups
        .iter()
        .all(|group| group.workspace_ids.is_empty()));
}

#[test]
fn grouped_header_toggle_and_workspace_drag_are_client_local() {
    let mut state = group_state();
    let id = state
        .config
        .preferences
        .space_groups
        .create("local", "Work")
        .unwrap();
    state
        .config
        .preferences
        .space_groups
        .assign("local", &["ws_1".into()], Some(&id))
        .unwrap();
    state.config.preferences.space_groups.grouped = true;
    state.compose(106, 30).unwrap();
    let header = state.hits.space_groups[0].0;
    let toggle = state.handle_raw_events(vec![RawInputEvent::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: header.x + 2,
        row: header.y,
        modifiers: KeyModifiers::empty(),
    })]);
    assert_local(&toggle);
    assert!(state.config.preferences.space_groups.groups[0].collapsed);
    state.compose(106, 30).unwrap();
    let row = state.hits.workspaces[0].rect;
    state.handle_raw_events(vec![RawInputEvent::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: row.x + 2,
        row: row.y,
        modifiers: KeyModifiers::empty(),
    })]);
    let drag = state.handle_raw_events(vec![RawInputEvent::Mouse(MouseEvent {
        kind: MouseEventKind::Drag(MouseButton::Left),
        column: row.x + 2,
        row: row.y + 3,
        modifiers: KeyModifiers::empty(),
    })]);
    assert_local(&drag);
    assert!(!matches!(
        state.chrome_drag,
        Some(ClientChromeDrag::Workspace { .. })
    ));
}

#[test]
fn group_header_rows_count_toward_workspace_reveal_scroll() {
    let mut state = group_state();
    state
        .config
        .preferences
        .space_groups
        .create("local", "Empty")
        .unwrap();
    let group = state
        .config
        .preferences
        .space_groups
        .create("local", "Work")
        .unwrap();
    state
        .config
        .preferences
        .space_groups
        .assign("local", &["ws_1".into()], Some(&group))
        .unwrap();
    state.config.preferences.space_groups.grouped = true;
    state.hits.workspace_max_scroll = 10;
    state.reveal_workspace("ws_1");
    assert_eq!(
        state.workspace_scroll, 2,
        "two headers precede the workspace"
    );
    assert_eq!(
        state
            .navigation_workspace_entries(state.snapshot.as_ref().unwrap())
            .len(),
        1
    );
}

#[test]
fn long_group_submenu_scrolls_selected_items_into_view_and_keeps_absolute_hits() {
    let mut state = group_state();
    for index in 0..24 {
        state
            .config
            .preferences
            .space_groups
            .create("local", &format!("Group {index:02}"))
            .unwrap();
    }
    state.open_workspace_context_menu("ws_1".into(), 1, 1);
    activate(&mut state, ClientContextMenuAction::MoveToSpaceGroup);
    state.move_context_menu_selection(25);
    state.compose(81, 15).unwrap();
    assert!(state.hits.context_menu_rows.first().unwrap().1 > 0);
    assert_eq!(state.hits.context_menu_rows.last().unwrap().1, 25);
    let visible_rows = state.hits.context_menu_rows.clone();
    let (first, index) = visible_rows[0];
    let hover = state.handle_raw_events(vec![RawInputEvent::Mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: first.x,
        row: first.y,
        modifiers: KeyModifiers::empty(),
    })]);
    assert_local(&hover);
    state.compose(81, 15).unwrap();
    assert_eq!(
        state.hits.context_menu_rows, visible_rows,
        "hovering within a page must not move the rows under the pointer"
    );
    let clicked = state.handle_raw_events(vec![RawInputEvent::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: first.x,
        row: first.y,
        modifiers: KeyModifiers::empty(),
    })]);
    assert_local(&clicked);
    assert_eq!(
        state.config.preferences.space_groups.groups[index - 2].workspace_ids,
        vec!["ws_1".to_owned()]
    );
    assert!(state
        .config
        .preferences
        .space_groups
        .groups
        .iter()
        .enumerate()
        .all(|(group_index, group)| group_index == index - 2 || group.workspace_ids.is_empty()));
}
