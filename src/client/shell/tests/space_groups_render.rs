//! Client-only grouping must preserve workspace identity and rendered navigation.
use super::*;
use crate::client::endpoint::{ClientEndpointStatus, ProfileId, SavedSshEndpoint};

fn populated_spaces(count: usize) -> ClientShellSnapshot {
    let mut projected = snapshot();
    let workspace_template = projected.workspaces[0].clone();
    let tab_template = projected.tabs[0].clone();
    let pane_template = projected.panes[0].clone();
    projected.workspaces.clear();
    projected.tabs.clear();
    projected.panes.clear();
    for index in 0..count {
        let number = index + 1;
        let mut workspace = workspace_template.clone();
        workspace.workspace_id = format!("ws_{number}");
        workspace.active_tab_id = format!("tab_{number}");
        workspace.number = number;
        workspace.label = format!("Space {number}");
        workspace.focused = index == 0;
        let mut tab = tab_template.clone();
        tab.workspace_id = workspace.workspace_id.clone();
        tab.tab_id = workspace.active_tab_id.clone();
        tab.focused = index == 0;
        let mut pane = pane_template.clone();
        pane.workspace_id = workspace.workspace_id.clone();
        pane.tab_id = tab.tab_id.clone();
        pane.pane_id = format!("pane_{number}");
        pane.focused = index == 0;
        projected.agents.push(ClientShellAgent {
            pane_id: pane.pane_id.clone(),
            workspace_id: workspace.workspace_id.clone(),
            tab_id: tab.tab_id.clone(),
            name: Some(format!("worker-{number}")),
            display_agent: None,
            agent: Some("codex".into()),
            title: None,
            terminal_title: None,
            terminal_title_stripped: None,
            agent_status: AgentStatus::Working,
            state_change_seq: number as u64,
            state_labels: Vec::new(),
            tokens: vec![("summary".into(), format!("Populated workspace {number}"))],
            focused: index == 0,
        });
        projected.workspaces.push(workspace);
        projected.tabs.push(tab);
        projected.panes.push(pane);
    }
    projected
}

fn local_state(projected: ClientShellSnapshot) -> ClientShellState {
    let mut state = ClientShellState::new(ClientShellConfig::from_config(&Config::default()));
    state.set_snapshot(Box::new(projected));
    state.set_pane_surface(surface());
    state.sidebar_section_split = 0.65;
    state
}

fn add_group(
    state: &mut ClientShellState,
    endpoint: &ClientEndpointId,
    name: &str,
    members: &[&str],
) -> String {
    let groups = &mut state.config.preferences.space_groups;
    let endpoint_key = endpoint.storage_key();
    let id = groups.create(&endpoint_key, name).unwrap();
    groups
        .assign(
            &endpoint_key,
            &members
                .iter()
                .map(|id| (*id).to_owned())
                .collect::<Vec<_>>(),
            Some(&id),
        )
        .unwrap();
    groups.grouped = true;
    id
}

fn collapse(state: &mut ClientShellState, id: &str) {
    state
        .config
        .preferences
        .space_groups
        .groups
        .iter_mut()
        .find(|group| group.id == id)
        .unwrap()
        .collapsed = true;
}

fn visible_ids(state: &ClientShellState, endpoint: &ClientEndpointId) -> Vec<String> {
    state
        .hits
        .workspaces
        .iter()
        .filter(|hit| &hit.endpoint_id == endpoint)
        .map(|hit| hit.workspace_id.clone())
        .collect()
}

fn group_rect(state: &ClientShellState, endpoint: &ClientEndpointId, id: &str) -> Rect {
    state
        .hits
        .space_groups
        .iter()
        .find(|(_, target, group)| target == endpoint && group == id)
        .map(|(rect, _, _)| *rect)
        .expect("group header must have its own endpoint-scoped hit")
}

#[test]
fn space_groups_flat_mode_preserves_worktree_order_and_indentation() {
    let mut projected = populated_spaces(4);
    for (index, linked) in [(0, false), (3, true)] {
        projected.workspaces[index].worktree = Some(ClientShellWorktree {
            key: "repo-family".into(),
            label: "repo".into(),
            is_linked_worktree: linked,
        });
    }
    let mut state = local_state(projected);
    state.compose(120, 50).unwrap();
    let original = state
        .hits
        .workspaces
        .iter()
        .map(|hit| (hit.workspace_id.clone(), hit.indented))
        .collect::<Vec<_>>();
    assert_eq!(
        original
            .iter()
            .map(|(id, _)| id.as_str())
            .collect::<Vec<_>>(),
        ["ws_1", "ws_4", "ws_2", "ws_3"]
    );
    add_group(&mut state, &ClientEndpointId::Local, "Projects", &["ws_2"]);
    state.config.preferences.space_groups.grouped = false;
    state.compose(120, 50).unwrap();
    assert!(state.hits.space_groups.is_empty());
    assert_eq!(
        state
            .hits
            .workspaces
            .iter()
            .map(|hit| (hit.workspace_id.clone(), hit.indented))
            .collect::<Vec<_>>(),
        original
    );
}

#[test]
fn space_groups_rendered_headers_and_collapse_are_endpoint_scoped() {
    let projected = populated_spaces(4);
    let mut state = local_state(projected.clone());
    let profile = SavedSshEndpoint {
        id: ProfileId::parse("0123456789abcdef0123456789abcdef").unwrap(),
        label: "Build".into(),
        target: "dev@build.example".into(),
        session: "agents".into(),
        enabled: true,
    };
    let remote = ClientEndpointId::Ssh(profile.id.clone());
    state.set_endpoint_catalog(&[profile]);
    state.set_endpoint_status(&remote, ClientEndpointStatus::Online);
    let mut remote_snapshot = projected;
    remote_snapshot.boot_id = "remote-boot".into();
    state.set_endpoint_snapshot(&remote, Box::new(remote_snapshot));
    let local_group = add_group(
        &mut state,
        &ClientEndpointId::Local,
        "Projects",
        &["ws_2", "ws_4"],
    );
    let remote_group = add_group(&mut state, &remote, "Projects", &["ws_2", "ws_4"]);
    state.compose(146, 80).unwrap();
    let local_header = group_rect(&state, &ClientEndpointId::Local, &local_group);
    let remote_header = group_rect(&state, &remote, &remote_group);
    assert_ne!(local_header.y, remote_header.y);
    for (header, endpoint) in [
        (local_header, &ClientEndpointId::Local),
        (remote_header, &remote),
    ] {
        assert!(!state.hits.workspaces.iter().any(|hit| {
            &hit.endpoint_id == endpoint && hit.rect.y <= header.y && header.y < hit.rect.bottom()
        }));
    }
    collapse(&mut state, &local_group);
    state.compose(146, 80).unwrap();
    assert_eq!(
        visible_ids(&state, &ClientEndpointId::Local),
        ["ws_1", "ws_3"]
    );
    assert_eq!(
        visible_ids(&state, &remote),
        ["ws_2", "ws_4", "ws_1", "ws_3"]
    );
    assert_eq!(state.active_endpoint_id, ClientEndpointId::Local);
    assert_eq!(
        state
            .snapshot
            .as_ref()
            .unwrap()
            .focused_workspace_id
            .as_deref(),
        Some("ws_1")
    );
}

#[test]
fn space_groups_collapsed_header_preserves_hidden_member_status_priority() {
    for (first, second, highest) in [
        (
            AgentStatus::Idle,
            AgentStatus::Working,
            AgentStatus::Working,
        ),
        (AgentStatus::Working, AgentStatus::Done, AgentStatus::Done),
        (
            AgentStatus::Done,
            AgentStatus::Blocked,
            AgentStatus::Blocked,
        ),
    ] {
        let mut projected = populated_spaces(3);
        let mut state = local_state(projected.clone());
        // Workspace status is derived from agent presentation. A newly observed
        // completion must advance its sequence after the initial snapshot.
        projected.revision += 1;
        for (index, status) in [(1, first), (2, second)] {
            projected.agents[index].agent_status = status;
            if status == AgentStatus::Done {
                projected.agents[index].state_change_seq += 1;
            }
        }
        state.set_snapshot(Box::new(projected));
        let mut current_surface = surface();
        current_surface.projection_revision = state.snapshot.as_ref().unwrap().revision;
        state.set_pane_surface(current_surface);
        assert_eq!(
            state.snapshot.as_ref().unwrap().workspaces[1].agent_status,
            first
        );
        assert_eq!(
            state.snapshot.as_ref().unwrap().workspaces[2].agent_status,
            second
        );
        let id = add_group(
            &mut state,
            &ClientEndpointId::Local,
            "Activity",
            &["ws_2", "ws_3"],
        );
        collapse(&mut state, &id);
        let frame = state.compose(120, 40).unwrap();
        assert_eq!(visible_ids(&state, &ClientEndpointId::Local), ["ws_1"]);
        let header = group_rect(&state, &ClientEndpointId::Local, &id);
        let buffer = frame.to_ratatui_buffer().unwrap();
        let color = status_color(highest, &state.config.palette);
        assert!(
            (header.x..header.right()).any(|x| buffer[(x, header.y)].fg == color),
            "the collapsed header must display its most urgent hidden status: {highest:?}"
        );
    }
}

#[test]
fn space_groups_worktree_family_stays_together_and_collapsed_focus_remains_visible() {
    let mut projected = populated_spaces(4);
    for (index, linked) in [(1, false), (3, true)] {
        projected.workspaces[index].worktree = Some(ClientShellWorktree {
            key: "project-family".into(),
            label: "project".into(),
            is_linked_worktree: linked,
        });
    }
    let mut state = local_state(projected.clone());
    let id = add_group(&mut state, &ClientEndpointId::Local, "Project", &["ws_2"]);
    state.compose(120, 50).unwrap();
    assert_eq!(
        visible_ids(&state, &ClientEndpointId::Local),
        ["ws_2", "ws_4", "ws_1", "ws_3"]
    );
    assert!(
        state
            .hits
            .workspaces
            .iter()
            .find(|hit| hit.workspace_id == "ws_4")
            .unwrap()
            .indented
    );
    collapse(&mut state, &id);
    state.compose(120, 50).unwrap();
    assert_eq!(
        visible_ids(&state, &ClientEndpointId::Local),
        ["ws_1", "ws_3"]
    );
    projected.focused_workspace_id = Some("ws_4".into());
    projected.focused_tab_id = Some("tab_4".into());
    projected.focused_pane_id = Some("pane_4".into());
    for workspace in &mut projected.workspaces {
        workspace.focused = workspace.workspace_id == "ws_4";
    }
    for tab in &mut projected.tabs {
        tab.focused = tab.tab_id == "tab_4";
    }
    for pane in &mut projected.panes {
        pane.focused = pane.pane_id == "pane_4";
    }
    for agent in &mut projected.agents {
        agent.focused = agent.pane_id == "pane_4";
    }
    state.set_snapshot(Box::new(projected));
    let mut focused_surface = surface();
    focused_surface.panes[0].pane_id = "pane_4".into();
    state.set_pane_surface(focused_surface);
    state.compose(120, 50).unwrap();
    assert_eq!(
        visible_ids(&state, &ClientEndpointId::Local),
        ["ws_4", "ws_1", "ws_3"]
    );
    assert!(
        !state
            .hits
            .workspaces
            .iter()
            .find(|hit| hit.workspace_id == "ws_4")
            .unwrap()
            .indented
    );
}

#[test]
fn space_groups_keyboard_navigation_follows_visible_group_order() {
    let mut state = local_state(populated_spaces(4));
    let group = add_group(
        &mut state,
        &ClientEndpointId::Local,
        "First",
        &["ws_2", "ws_4"],
    );
    add_group(&mut state, &ClientEndpointId::Local, "Second", &["ws_3"]);
    for collapsed in [false, true] {
        state
            .config
            .preferences
            .space_groups
            .groups
            .iter_mut()
            .find(|g| g.id == group)
            .unwrap()
            .collapsed = collapsed;
        state.compose(120, 50).unwrap();
        let ids = visible_ids(&state, &ClientEndpointId::Local);
        assert_eq!(
            ids,
            if collapsed {
                vec!["ws_3", "ws_1"]
            } else {
                vec!["ws_2", "ws_4", "ws_3", "ws_1"]
            }
        );
        state.navigate_workspace_id = state.navigation_target(&ClientEndpointId::Local, &ids[0]);
        for next in ids.iter().cycle().skip(1).take(ids.len()) {
            state.move_navigate_workspace(1);
            assert!(state
                .navigate_workspace_id
                .as_ref()
                .unwrap()
                .matches(&ClientEndpointId::Local, next));
        }
    }
}

#[test]
fn space_groups_narrow_render_keeps_header_and_workspace_hits_in_bounds() {
    let mut state = local_state(populated_spaces(15));
    let first = add_group(
        &mut state,
        &ClientEndpointId::Local,
        "Research and development",
        &["ws_2", "ws_3", "ws_4"],
    );
    add_group(
        &mut state,
        &ClientEndpointId::Local,
        "Tests and reviews",
        &["ws_5", "ws_6"],
    );
    for collapsed in [false, true] {
        state
            .config
            .preferences
            .space_groups
            .groups
            .iter_mut()
            .find(|g| g.id == first)
            .unwrap()
            .collapsed = collapsed;
        for (cols, rows) in [(72, 10), (80, 20), (120, 40), (146, 80)] {
            let frame = state.compose(cols, rows).unwrap();
            assert_eq!((frame.width, frame.height), (cols, rows));
            for rect in state
                .hits
                .space_groups
                .iter()
                .map(|(rect, _, _)| rect)
                .chain(state.hits.workspaces.iter().map(|hit| &hit.rect))
            {
                assert!(!rect.is_empty());
                assert!(
                    rect.right() <= cols && rect.bottom() <= rows,
                    "{cols}x{rows}: {rect:?}"
                );
            }
            assert!(state.hits.spaces_grouping_toggle.right() <= cols);
            assert!(state.hits.spaces_grouping_toggle.bottom() <= rows);
        }
    }
}

#[test]
#[ignore = "non-gating fixed-geometry Spaces grouping composition profile"]
fn space_groups_render_scale_profile() {
    const COLS: u16 = 146;
    const ROWS: u16 = 80;
    for count in [1, 15] {
        let projected = populated_spaces(count);
        let mut baseline_ns = 0.0;
        for grouped in [false, true] {
            let mut state = local_state(projected.clone());
            state.sidebar_section_split = 0.5;
            let ids = projected
                .workspaces
                .iter()
                .map(|w| w.workspace_id.as_str())
                .collect::<Vec<_>>();
            add_group(
                &mut state,
                &ClientEndpointId::Local,
                "Populated spaces",
                &ids,
            );
            state.config.preferences.space_groups.grouped = grouped;
            for _ in 0..20 {
                std::hint::black_box(state.compose(COLS, ROWS).expect("group profile warmup"));
            }
            assert_eq!(
                state.hits.workspaces.len(),
                count,
                "all measured workspaces must render"
            );
            assert_eq!(
                state.hits.agents.len(),
                count,
                "all measured populated agents must render"
            );
            let mut samples = Vec::with_capacity(200);
            for _ in 0..200 {
                let started = std::time::Instant::now();
                std::hint::black_box(state.compose(COLS, ROWS).expect("group profile frame"));
                samples.push(started.elapsed().as_nanos());
            }
            samples.sort_unstable();
            let median_ns = samples[100] as f64;
            if !grouped {
                baseline_ns = median_ns;
            }
            eprintln!(
                "space-groups panes={count} geometry={COLS}x{ROWS} mode={} median_us={:.1} p95_us={:.1} relative_to_flat={:.3}",
                if grouped { "Groups" } else { "Flat" }, median_ns / 1000.0,
                samples[190] as f64 / 1000.0, median_ns / baseline_ns.max(1.0)
            );
        }
    }
}
