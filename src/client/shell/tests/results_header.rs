use super::*;

fn header_state(count: u16) -> ClientShellState {
    let mut projected = snapshot();
    let mut source = surface();
    let template_pane = projected.panes[0].clone();
    let template_surface = source.panes[0].clone();
    projected.panes.clear();
    source.panes.clear();
    let mut buffer = Buffer::empty(Rect::new(0, 0, 80, 24));
    for index in 0..count {
        let pane_id = format!("pane_{}", index + 1);
        let mut pane = template_pane.clone();
        pane.pane_id = pane_id.clone();
        pane.label = Some(format!("worker-{index}"));
        pane.focused = index == 0;
        projected.agents.push(ClientShellAgent {
            pane_id: pane_id.clone(),
            workspace_id: pane.workspace_id.clone(),
            tab_id: pane.tab_id.clone(),
            name: pane.label.clone(),
            display_agent: None,
            agent: Some("codex".into()),
            title: None,
            terminal_title: None,
            terminal_title_stripped: None,
            agent_status: AgentStatus::Idle,
            state_change_seq: 0,
            state_labels: Vec::new(),
            tokens: Vec::new(),
            focused: pane.focused,
        });
        projected.panes.push(pane);
        let rect = Rect::new(index * (80 / count), 0, 80 / count, 24);
        let mut geometry = template_surface.clone();
        geometry.pane_id = pane_id;
        geometry.focused = index == 0;
        geometry.rect = SurfaceRect {
            x: rect.x,
            y: rect.y,
            width: rect.width,
            height: rect.height,
        };
        geometry.inner_rect = SurfaceRect {
            x: rect.x + 1,
            y: 1,
            width: rect.width - 2,
            height: 22,
        };
        ratatui::widgets::Widget::render(
            ratatui::widgets::Block::bordered().title(format!(" worker-{index} ")),
            rect,
            &mut buffer,
        );
        buffer.set_string(rect.x + 1, 1, "Agent content", Style::default());
        source.panes.push(geometry);
    }
    source.frame = FrameData::from_ratatui_buffer(&buffer, None);
    let mut state = ClientShellState::new(ClientShellConfig::from_config(&Config::default()));
    state.set_endpoint_methods(Some(vec!["plugin.pane.open".into()]));
    state.set_snapshot(Box::new(projected));
    state.set_pane_surface(source);
    state
}

fn click(state: &mut ClientShellState, rect: Rect) -> ClientShellInput {
    state.handle_raw_events(vec![RawInputEvent::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: rect.x + 1,
        row: rect.y,
        modifiers: KeyModifiers::empty(),
    })])
}

#[test]
fn results_header_routes_clicked_split_not_focused_pane() {
    for count in [1, 2] {
        let mut state = header_state(count);
        let frame = state.compose(106, 30).unwrap();
        assert_eq!(state.hits.results_buttons.len(), usize::from(count));
        assert_eq!(
            frame_rows(&frame).join("\n").matches("[Results]").count(),
            usize::from(count)
        );
        let (rect, pane_id) = state.hits.results_buttons.last().unwrap().clone();
        let opened = click(&mut state, rect);
        assert!(opened.requests.is_empty(), "must not send terminal input");
        let [ClientShellAction::Endpoint { request, .. }] = opened.actions.as_slice() else {
            panic!("one public request expected")
        };
        let crate::api::schema::Method::PluginPaneOpen(params) = &request.method else {
            panic!("plugin popup expected")
        };
        assert_eq!(params.env.get("WATCHTOWER_RESULTS_PANE"), Some(&pane_id));
        assert!(params.target_pane_id.is_none());
        assert_eq!(params.plugin_id, "watchtower-results");
        assert!(state.chrome_drag.is_none());
        assert!(state.selection.is_none());
        assert!(state.popup_pending);
        assert!(
            click(&mut state, rect).actions.is_empty(),
            "no duplicate popup requests"
        );
    }
}

#[test]
fn results_header_does_not_receive_clicks_through_popup_or_menu() {
    let mut state = header_state(2);
    state.compose(106, 30).unwrap();
    let rect = state.hits.results_buttons[1].0;
    state.toggle_global_menu();
    state.compose(106, 30).unwrap();
    assert!(click(&mut state, rect).actions.is_empty());
    let mut source = state.pane_surface.as_ref().unwrap().clone();
    source.popup = surface_with_popup().popup;
    source.surface_revision += 1;
    state.set_pane_surface(source);
    state.compose(106, 30).unwrap();
    assert!(state.hits.results_buttons.is_empty());
    assert!(click(&mut state, rect).actions.is_empty());
}

#[test]
fn results_header_stale_hit_cannot_open_removed_pane() {
    let mut state = header_state(2);
    state.compose(106, 30).unwrap();
    let mut projected = (**state.snapshot.as_ref().unwrap()).clone();
    projected.panes.pop();
    projected.agents.pop();
    projected.revision += 1;
    state.set_snapshot(Box::new(projected));
    let mut outcome = ClientShellInput::default();
    state.open_results_for_pane("pane_2".into(), &mut outcome);
    assert!(outcome.actions.is_empty());
    assert!(!state.popup_pending);
}

#[test]
fn results_header_survives_terminal_row_fast_patch_without_content_changes() {
    let mut state = header_state(2);
    let initial = state.compose(106, 30).unwrap();
    let source = state.pane_surface.as_ref().unwrap();
    let mut cell = source.frame.cells[usize::from(source.frame.width) + 1].clone();
    cell.symbol = "X".into();
    let patch = crate::protocol::PaneSurfacePatch {
        boot_id: source.boot_id.clone(),
        projection_revision: source.projection_revision,
        base_surface_revision: source.surface_revision,
        surface_revision: source.surface_revision + 1,
        rows: vec![crate::protocol::PaneSurfacePatchRow {
            x: 1,
            y: 1,
            cells: vec![cell],
        }],
        panes: vec![source.panes[0].clone()],
        cursor: None,
    };
    let ClientPaneSurfacePatchOutcome::Applied(Some(patch)) = state.apply_pane_surface_patch(patch)
    else {
        panic!("terminal patch must retain fast path")
    };
    let patched = apply_composed_surface_patch(&initial, patch).unwrap();
    let recomposed = state.compose(106, 30).unwrap();
    assert_eq!(patched, recomposed);
    assert_eq!(
        frame_rows(&patched).join("\n").matches("[Results]").count(),
        2
    );
    assert!(frame_rows(&patched).join("\n").contains("Xgent content"));
}
