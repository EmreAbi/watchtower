use super::*;
use crate::api::schema::{AgentViewField, AgentViewFilter, AgentViewSetParams, AgentViewValue};
use crate::client::endpoint::{ProfileId, SavedSshEndpoint};
use crate::config::AgentPanelSortConfig;
use crate::input::{KeybindAction, KeybindMatch};

fn projected_agents() -> ClientShellSnapshot {
    let mut projected = snapshot();
    let mut workspace = projected.workspaces[0].clone();
    workspace.workspace_id = "ws_2".into();
    workspace.active_tab_id = "tab_2".into();
    workspace.number = 2;
    workspace.focused = false;
    projected.workspaces.push(workspace);
    let mut tab = projected.tabs[0].clone();
    tab.tab_id = "tab_2".into();
    tab.workspace_id = "ws_2".into();
    tab.focused = false;
    projected.tabs.push(tab);
    let template = projected.panes[0].clone();
    projected.panes.clear();
    for (id, workspace, tab, role, name) in [
        ("pane_1", "ws_1", "tab_1", "⚙ WORKER", "Zulu"),
        ("pane_2", "ws_2", "tab_2", "👤 CONTROL", "Hidden"),
        ("pane_3", "ws_1", "tab_1", "🔎 REVIEW", "Alpha"),
    ] {
        projected.panes.push(ClientShellPane {
            pane_id: id.into(),
            workspace_id: workspace.into(),
            tab_id: tab.into(),
            focused: id == "pane_1",
            ..template.clone()
        });
        projected.agents.push(ClientShellAgent {
            pane_id: id.into(),
            workspace_id: workspace.into(),
            tab_id: tab.into(),
            name: Some(name.into()),
            display_agent: None,
            agent: Some("codex".into()),
            title: None,
            terminal_title: None,
            terminal_title_stripped: None,
            agent_status: AgentStatus::Idle,
            state_change_seq: projected.agents.len() as u64,
            state_labels: Vec::new(),
            tokens: vec![("team_role".into(), role.into())],
            focused: id == "pane_1",
        });
    }
    projected
}

fn filtered_config() -> ClientShellConfig {
    let mut config = Config::default();
    config.ui.agent_panel_sort = AgentPanelSortConfig::Role;
    config.ui.sidebar.agents.rows = vec![vec![
        crate::config::AgentSidebarToken::Custom("team_role".into()),
        crate::config::AgentSidebarToken::Agent,
    ]];
    let mut client = ClientShellConfig::from_config(&config);
    client.agent_current_workspace_only = true;
    client
}

fn remote_state() -> (ClientShellState, ClientEndpointId) {
    let mut state = ClientShellState::new(filtered_config());
    let profile = SavedSshEndpoint {
        id: ProfileId::parse("0123456789abcdef0123456789abcdef").unwrap(),
        label: "Remote".into(),
        target: "dev@remote.example".into(),
        session: "agents".into(),
        enabled: true,
    };
    let remote = ClientEndpointId::Ssh(profile.id.clone());
    state.set_endpoint_catalog(&[profile]);
    state.set_snapshot(Box::new(projected_agents()));
    let mut remote_snapshot = projected_agents();
    remote_snapshot.boot_id = "remote-boot".into();
    state.set_endpoint_snapshot(&remote, Box::new(remote_snapshot));
    state.set_endpoint_status(&ClientEndpointId::Local, ClientEndpointStatus::Online);
    state.set_endpoint_status(&remote, ClientEndpointStatus::Online);
    (state, remote)
}

#[test]
fn workspace_agent_filter_preserves_all_sorts_and_intersects_custom_order() {
    let mut projected = projected_agents();
    let mut config = filtered_config();
    for sort in [
        AgentPanelSortConfig::Spaces,
        AgentPanelSortConfig::Priority,
        AgentPanelSortConfig::Role,
        AgentPanelSortConfig::Name,
        AgentPanelSortConfig::Recent,
    ] {
        config.agent_panel_sort = sort;
        let expected = agent_sidebar::ordered_agent_pane_ids(&projected, sort)
            .into_iter()
            .filter(|id| id != "pane_2")
            .collect::<Vec<_>>();
        assert_eq!(
            agent_sidebar::visible_agent_pane_ids(&projected, &config),
            expected
        );
    }
    projected.agent_view_label = Some("custom".into());
    projected.agent_order = vec!["pane_2".into(), "pane_3".into()];
    assert_eq!(
        agent_sidebar::visible_agent_pane_ids(&projected, &config),
        ["pane_3"]
    );
    config.agent_current_workspace_only = false;
    assert_eq!(
        agent_sidebar::visible_agent_pane_ids(&projected, &config),
        ["pane_2", "pane_3"]
    );
}

#[test]
fn workspace_agent_filter_tracks_workspace_changes_and_missing_focus() {
    let mut projected = projected_agents();
    let config = filtered_config();
    assert_eq!(
        agent_sidebar::visible_agent_pane_ids(&projected, &config),
        ["pane_1", "pane_3"]
    );
    projected.focused_workspace_id = Some("ws_2".into());
    assert_eq!(
        agent_sidebar::visible_agent_pane_ids(&projected, &config),
        ["pane_2"]
    );
    for focused in [None, Some("empty".into())] {
        projected.focused_workspace_id = focused;
        assert!(agent_sidebar::visible_agent_pane_ids(&projected, &config).is_empty());
    }
}

#[test]
fn workspace_agent_filter_local_render_and_keyboard_use_the_same_scope() {
    let mut state = ClientShellState::new(filtered_config());
    state.set_snapshot(Box::new(projected_agents()));
    state.set_pane_surface(surface());
    for collapsed in [false, true] {
        state.sidebar_collapsed = collapsed;
        let frame = state.compose(146, 50).expect("agent frame");
        assert_eq!(
            state
                .hits
                .agents
                .iter()
                .map(|(_, id)| id.as_str())
                .collect::<Vec<_>>(),
            ["pane_1", "pane_3"]
        );
        if !collapsed {
            let text = frame_rows(&frame).join("\n");
            assert!(text.contains("workspace ▾"), "{text}");
            assert!(text.contains("WORKER") && text.contains("REVIEW"), "{text}");
            assert!(!text.contains("CONTROL"), "{text}");
        }
        for (action, expected) in [
            (KeybindAction::FocusAgent(0), "pane_1"),
            (KeybindAction::FocusAgent(1), "pane_3"),
            (KeybindAction::NextAgent, "pane_3"),
            (KeybindAction::PreviousAgent, "pane_3"),
        ] {
            assert!(matches!(state.endpoint_method_for_action(action),
                Some(crate::api::schema::Method::PaneFocus(target)) if target.pane_id == expected));
        }
        assert!(state
            .endpoint_method_for_action(KeybindAction::FocusAgent(2))
            .is_none());
        assert!(!state
            .indexed_navigation_target_exists(&KeybindMatch::Action(KeybindAction::FocusAgent(2))));
    }
}

#[test]
fn workspace_agent_filter_empty_panel_is_explicit_and_has_no_navigation() {
    let mut projected = projected_agents();
    projected.focused_workspace_id = Some("empty".into());
    let mut state = ClientShellState::new(filtered_config());
    state.set_snapshot(Box::new(projected.clone()));
    let area = Rect::new(0, 0, 40, 12);
    let mut buffer = Buffer::empty(area);
    let mut hits = ShellHitMap::default();
    let mut scroll = 99;
    agent_sidebar::render_agent_panel(
        &mut buffer,
        area,
        &projected,
        &state.config,
        &mut scroll,
        &mut hits,
    );
    let text = (0..area.height)
        .map(|y| {
            (0..area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n");
    assert!(text.contains("no agents in workspace"), "{text}");
    assert!(hits.agents.is_empty());
    assert_eq!(scroll, 0);
    for action in [
        KeybindAction::FocusAgent(0),
        KeybindAction::NextAgent,
        KeybindAction::PreviousAgent,
    ] {
        assert!(state.endpoint_method_for_action(action).is_none());
    }
    let narrow = Rect::new(0, 0, 12, 3);
    agent_sidebar::render_agent_panel_header(&mut buffer, narrow, None, &state.config, &mut hits);
    assert_eq!(buffer[(11, 1)].symbol(), "▾");
    agent_sidebar::render_agent_panel_header(
        &mut buffer,
        area,
        Some("custom"),
        &state.config,
        &mut hits,
    );
    assert!(!hits.agent_sort_toggle.is_empty());
}

#[test]
fn workspace_agent_filter_aggregate_identity_includes_endpoint_and_active_workspace() {
    let (mut state, remote) = remote_state();
    for projection_supported in [false, true] {
        for endpoint in &mut state.endpoints {
            endpoint.agent_view_projection_supported = projection_supported;
        }
        for active in [&ClientEndpointId::Local, &remote] {
            let rows = aggregate_navigation::visible_aggregate_agent_rows(
                &state.endpoints,
                active,
                &state.config,
            );
            assert_eq!(
                rows.iter()
                    .map(|row| row.agent.pane_id.as_str())
                    .collect::<Vec<_>>(),
                ["pane_1", "pane_3"]
            );
            assert!(rows.iter().all(|row| row.endpoint.endpoint_id == active));
        }
    }
    let mut changed = projected_agents();
    changed.boot_id = "remote-boot".into();
    changed.focused_workspace_id = Some("ws_2".into());
    state.set_endpoint_snapshot(&remote, Box::new(changed));
    state.agent_scroll = 7;
    assert!(state.activate_endpoint_projection(&remote));
    assert_eq!(state.agent_scroll, 0);
    let targets = aggregate_navigation::visible_online_agent_targets(
        &state.endpoints,
        &state.active_endpoint_id,
        &state.config,
    );
    assert_eq!(targets.len(), 1);
    assert_eq!(targets[0].endpoint_id, remote);
    assert_eq!(targets[0].pane_id, "pane_2");
    state.set_endpoint_status(&remote, ClientEndpointStatus::Reconnecting);
    assert!(aggregate_navigation::visible_online_agent_targets(
        &state.endpoints,
        &remote,
        &state.config
    )
    .is_empty());
    assert_eq!(
        aggregate_navigation::visible_aggregate_agent_rows(
            &state.endpoints,
            &remote,
            &state.config
        )
        .len(),
        1
    );
}

#[test]
fn workspace_agent_filter_resets_scroll_only_when_the_scoped_list_changes() {
    for filtered in [false, true] {
        let (mut state, remote) = remote_state();
        state.config.agent_current_workspace_only = filtered;
        state.agent_scroll = 7;
        let mut changed = projected_agents();
        changed.focused_workspace_id = Some("ws_2".into());
        state.set_snapshot(Box::new(changed));
        assert_eq!(state.agent_scroll, if filtered { 0 } else { 7 });

        state.set_snapshot(Box::new(projected_agents()));
        state.agent_scroll = 7;
        // Both machines focus ws_1, but they contain different agent instances.
        assert!(state.activate_endpoint_projection(&remote));
        assert_eq!(
            state
                .snapshot
                .as_ref()
                .unwrap()
                .focused_workspace_id
                .as_deref(),
            Some("ws_1")
        );
        assert_eq!(state.agent_scroll, if filtered { 0 } else { 7 });
    }
}

#[test]
fn workspace_agent_filter_aggregate_render_navigation_and_custom_view_intersect() {
    let (mut state, remote) = remote_state();
    let area = Rect::new(0, 0, 40, 25);
    for collapsed in [false, true] {
        let mut buffer = Buffer::empty(area);
        let mut hits = ShellHitMap::default();
        if collapsed {
            endpoint_agents::render_collapsed(
                &mut buffer,
                area,
                &state.endpoints,
                &state.active_endpoint_id,
                &state.config,
                &mut hits,
            );
        } else {
            endpoint_agents::render_expanded(
                &mut buffer,
                area,
                None,
                &state.endpoints,
                &state.active_endpoint_id,
                &state.config,
                &mut 0,
                &mut hits,
            );
        }
        assert_eq!(
            hits.endpoint_agents
                .iter()
                .map(|(_, endpoint, pane)| (endpoint, pane.as_str()))
                .collect::<Vec<_>>(),
            [
                (&ClientEndpointId::Local, "pane_1"),
                (&ClientEndpointId::Local, "pane_3")
            ]
        );
    }
    for (action, expected) in [
        (KeybindAction::FocusAgent(1), "pane_3"),
        (KeybindAction::NextAgent, "pane_3"),
        (KeybindAction::PreviousAgent, "pane_3"),
    ] {
        let mut outcome = ClientShellInput::default();
        assert!(state.handle_endpoint_navigation(action, &mut outcome));
        assert!(
            matches!(outcome.actions.as_slice(), [ClientShellAction::ActivateEndpoint {
            endpoint_id: ClientEndpointId::Local, target: Some(ClientEndpointFocusTarget::Pane(pane))
        }] if pane == expected)
        );
    }
    state.set_test_endpoint_agent_view(
        &ClientEndpointId::Local,
        Some(AgentViewSetParams {
            source: "tests.workspace-scope".into(),
            label: Some("review only".into()),
            filter: Some(AgentViewFilter::Eq {
                field: AgentViewField::Token {
                    token: "team_role".into(),
                },
                value: AgentViewValue::String("🔎 REVIEW".into()),
            }),
            sort: Vec::new(),
        }),
    );
    let targets = aggregate_navigation::visible_online_agent_targets(
        &state.endpoints,
        &state.active_endpoint_id,
        &state.config,
    );
    assert_eq!(targets.len(), 1);
    assert_eq!(targets[0].pane_id, "pane_3");
    assert_ne!(targets[0].endpoint_id, remote);
    assert!(!state
        .indexed_navigation_target_exists(&KeybindMatch::Action(KeybindAction::FocusAgent(1))));
}

#[test]
#[ignore = "non-gating fixed-geometry workspace agent filter composition profile"]
fn workspace_agent_filter_render_scale_profile() {
    const COLS: u16 = 146;
    const ROWS: u16 = 80;
    for count in [1, 15] {
        let mut projected = projected_agents();
        let agent = projected.agents[0].clone();
        let pane = projected.panes[0].clone();
        projected.agents.clear();
        projected.panes.clear();
        for index in 0..count {
            let id = format!("pane_{}", index + 1);
            projected.agents.push(ClientShellAgent {
                pane_id: id.clone(),
                name: Some(format!("agent-{index}")),
                focused: index == 0,
                ..agent.clone()
            });
            projected.panes.push(ClientShellPane {
                pane_id: id,
                focused: index == 0,
                ..pane.clone()
            });
        }
        let mut baseline_ns = 0.0;
        for filtered in [false, true] {
            let mut config = filtered_config();
            config.agent_current_workspace_only = filtered;
            let mut state = ClientShellState::new(config);
            state.set_snapshot(Box::new(projected.clone()));
            state.set_pane_surface(surface());
            for _ in 0..20 {
                std::hint::black_box(state.compose(COLS, ROWS).unwrap());
            }
            assert_eq!(state.hits.agents.len(), count);
            let mut samples = Vec::with_capacity(200);
            for _ in 0..200 {
                let start = std::time::Instant::now();
                std::hint::black_box(state.compose(COLS, ROWS).unwrap());
                samples.push(start.elapsed().as_nanos());
            }
            samples.sort_unstable();
            let median_ns = samples[100] as f64;
            if !filtered {
                baseline_ns = median_ns;
            }
            eprintln!(
                "workspace-agent-filter agents={count} geometry={COLS}x{ROWS} enabled={filtered} median_us={:.1} p95_us={:.1} relative_to_all={:.3}",
                median_ns / 1000.0,
                samples[190] as f64 / 1000.0,
                median_ns / baseline_ns.max(1.0)
            );
        }
    }
}
