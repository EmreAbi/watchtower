// Watchtower modifications: independent preview identity, isolation or role presentation.
use std::collections::{HashMap, HashSet};

use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Modifier, Style},
    text::Line,
    widgets::{Paragraph, Widget},
};

use super::*;

pub(super) struct AgentRow {
    pub(super) pane_id: String,
    pub(super) status: crate::api::schema::AgentStatus,
    pub(super) focused: bool,
    pub(super) rows: Vec<Vec<crate::ui::ResolvedToken>>,
}

/// Canonical team metadata only: names and other tokens never imply a role.
pub(super) fn agent_role_rank(tokens: &[(String, String)]) -> u8 {
    let Some((_, value)) = tokens.iter().find(|(key, _)| key == "team_role") else {
        return 3;
    };
    let role = value
        .trim()
        .trim_start_matches(['👤', '⚙', '🔎', '\u{fe0f}'])
        .trim();
    if role.eq_ignore_ascii_case("control") || role.eq_ignore_ascii_case("controller") {
        0
    } else if role.eq_ignore_ascii_case("worker") {
        1
    } else if role.eq_ignore_ascii_case("review") || role.eq_ignore_ascii_case("reviewer") {
        2
    } else {
        3
    }
}

pub(super) fn ordered_agent_pane_ids(
    snapshot: &ClientShellSnapshot,
    sort: crate::config::AgentPanelSortConfig,
) -> Vec<String> {
    if snapshot.agent_view_label.is_some() {
        return snapshot
            .agent_order
            .iter()
            .filter(|pane_id| {
                snapshot
                    .agents
                    .iter()
                    .any(|agent| agent.pane_id == pane_id.as_str())
            })
            .cloned()
            .collect();
    }
    let mut agents = snapshot.agents.iter().collect::<Vec<_>>();
    if sort == crate::config::AgentPanelSortConfig::Priority {
        agents.sort_by_key(|agent| {
            (
                std::cmp::Reverse(status_priority(agent.agent_status)),
                std::cmp::Reverse(agent.state_change_seq),
            )
        });
    } else if sort == crate::config::AgentPanelSortConfig::Role {
        agents.sort_by_key(|agent| agent_role_rank(&agent.tokens));
    } else if sort == crate::config::AgentPanelSortConfig::Recent {
        agents.sort_by_key(|agent| std::cmp::Reverse(agent.state_change_seq));
    } else if sort == crate::config::AgentPanelSortConfig::Name {
        agents.sort_by_cached_key(|agent| agent_sort_name(agent));
    }
    agents
        .into_iter()
        .map(|agent| agent.pane_id.clone())
        .collect()
}

/// Apply the client-only workspace scope after the selected view and sort.
pub(super) fn visible_agent_pane_ids(
    snapshot: &ClientShellSnapshot,
    config: &ClientShellConfig,
) -> Vec<String> {
    let mut panes = ordered_agent_pane_ids(snapshot, config.agent_panel_sort);
    if config.agent_current_workspace_only {
        let visible = snapshot
            .agents
            .iter()
            .filter(|agent| {
                Some(agent.workspace_id.as_str()) == snapshot.focused_workspace_id.as_deref()
            })
            .map(|agent| agent.pane_id.as_str())
            .collect::<HashSet<_>>();
        panes.retain(|pane_id| visible.contains(pane_id.as_str()));
    }
    panes
}

pub(super) fn agent_empty_message(
    agent_view_label: Option<&str>,
    config: &ClientShellConfig,
) -> Option<&'static str> {
    if config.agent_current_workspace_only {
        Some(" no agents in workspace")
    } else {
        agent_view_label.map(|_| " no matching agents")
    }
}

pub(super) fn agent_sort_name(agent: &crate::protocol::ClientShellAgent) -> String {
    agent
        .display_agent
        .as_deref()
        .or(agent.name.as_deref())
        .or(agent.agent.as_deref())
        .or(agent.title.as_deref())
        .unwrap_or("")
        .to_lowercase()
}

pub(super) fn render_agent_panel(
    buffer: &mut Buffer,
    area: Rect,
    snapshot: &ClientShellSnapshot,
    config: &ClientShellConfig,
    agent_scroll: &mut usize,
    hits: &mut ShellHitMap,
) {
    if !render_agent_panel_header(
        buffer,
        area,
        snapshot.agent_view_label.as_deref(),
        config,
        hits,
    ) {
        return;
    }

    let rows = agent_rows(snapshot, config, None);
    render_agent_list(
        buffer,
        area,
        &rows,
        agent_empty_message(snapshot.agent_view_label.as_deref(), config),
        config,
        agent_scroll,
        hits,
        |row| row.rows.len(),
        |buffer, rect, row, hits| {
            hits.agents.push((rect, row.pane_id.clone()));
            render_agent_row(buffer, rect, row, config);
        },
    );
}

pub(super) fn render_agent_panel_header(
    buffer: &mut Buffer,
    area: Rect,
    agent_view_label: Option<&str>,
    config: &ClientShellConfig,
    hits: &mut ShellHitMap,
) -> bool {
    if area.height == 0 {
        return false;
    }
    put_text(
        buffer,
        area.x,
        area.y,
        area.width,
        &"─".repeat(area.width as usize),
        Style::default().fg(config.palette.surface_dim),
    );
    if area.height < 2 {
        return false;
    }
    put_text(
        buffer,
        area.x,
        area.y + 1,
        area.width,
        " agents",
        Style::default()
            .fg(config.palette.overlay0)
            .add_modifier(Modifier::BOLD),
    );
    let sort_label = agent_view_label.unwrap_or(if config.agent_current_workspace_only {
        "workspace ▾"
    } else {
        match config.agent_panel_sort {
            crate::config::AgentPanelSortConfig::Spaces => "grouped ▾",
            crate::config::AgentPanelSortConfig::Priority => "priority ▾",
            crate::config::AgentPanelSortConfig::Role => "role ▾",
            crate::config::AgentPanelSortConfig::Recent => "recent ▾",
            crate::config::AgentPanelSortConfig::Name => "name ▾",
        }
    });
    #[cfg(feature = "watchtower")]
    let custom_label = agent_view_label.map(|label| {
        if config.agent_current_workspace_only {
            format!("{label} · workspace ▾")
        } else {
            format!("{label} ▾")
        }
    });
    #[cfg(feature = "watchtower")]
    let sort_label = custom_label.as_deref().unwrap_or(sort_label);
    let menu_enabled = agent_view_label.is_none() || cfg!(feature = "watchtower");
    // Keep the section label legible on narrow sidebars and retain a clickable arrow.
    let available = area.width.saturating_sub(9);
    let sort_label = if menu_enabled && display_width(sort_label) > usize::from(available) {
        if config.agent_current_workspace_only
            && display_width("workspace ▾") <= usize::from(available)
        {
            "workspace ▾"
        } else {
            "▾"
        }
    } else {
        sort_label
    };
    let sort_width = display_width(sort_label).min(usize::from(available)) as u16;
    let sort_rect = Rect::new(
        area.right().saturating_sub(sort_width),
        area.y + 1,
        sort_width,
        1,
    );
    hits.agent_sort_toggle = if config.mouse_capture && menu_enabled {
        sort_rect
    } else {
        Rect::default()
    };
    put_text(
        buffer,
        sort_rect.x,
        sort_rect.y,
        sort_rect.width,
        sort_label,
        Style::default()
            .fg(if agent_view_label.is_some() {
                config.palette.accent
            } else {
                config.palette.overlay0
            })
            .add_modifier(Modifier::BOLD),
    );
    true
}

pub(super) fn render_agent_list<T>(
    buffer: &mut Buffer,
    area: Rect,
    rows: &[T],
    empty_message: Option<&str>,
    config: &ClientShellConfig,
    agent_scroll: &mut usize,
    hits: &mut ShellHitMap,
    row_lines: impl Fn(&T) -> usize,
    mut render_row: impl FnMut(&mut Buffer, Rect, &T, &mut ShellHitMap),
) {
    let body = Rect::new(
        area.x,
        area.y.saturating_add(3),
        area.width,
        area.height.saturating_sub(3),
    );
    hits.agent_body = body;
    if body.is_empty() || rows.is_empty() {
        *agent_scroll = 0;
        if let Some(message) = empty_message.filter(|_| !body.is_empty()) {
            put_text(
                buffer,
                body.x,
                body.y,
                body.width,
                message,
                Style::default()
                    .fg(config.palette.overlay0)
                    .add_modifier(Modifier::DIM),
            );
        }
        return;
    }

    let row_heights = rows
        .iter()
        .map(|row| row_lines(row).max(1).min(u16::MAX as usize) as u16)
        .collect::<Vec<_>>();
    let gaps = rows
        .iter()
        .enumerate()
        .map(|(index, _)| {
            if index + 1 < rows.len() {
                config.agents.row_gap
            } else {
                0
            }
        })
        .collect::<Vec<_>>();
    let metrics =
        super::scroll::list_scroll_metrics(&row_heights, &gaps, body.height, *agent_scroll);
    hits.agent_max_scroll = metrics.max_offset_from_bottom;
    hits.agent_scroll_metrics = Some(metrics);
    *agent_scroll = metrics
        .max_offset_from_bottom
        .saturating_sub(metrics.offset_from_bottom);
    let show_scrollbar = metrics.max_offset_from_bottom > 0 && body.width > 1;
    let content_width = body.width.saturating_sub(u16::from(show_scrollbar));
    let mut y = body.y;
    for (index, row) in rows.iter().enumerate().skip(*agent_scroll) {
        let height = row_heights[index].min(body.height);
        if y.saturating_add(height) > body.bottom() {
            break;
        }
        let rect = Rect::new(body.x, y, content_width, height);
        render_row(buffer, rect, row, hits);
        y = y
            .saturating_add(height)
            .saturating_add(if index + 1 < rows.len() {
                config.agents.row_gap
            } else {
                0
            });
    }

    if show_scrollbar {
        let track = Rect::new(body.right().saturating_sub(1), body.y, 1, body.height);
        hits.agent_scrollbar = track;
        super::scroll::render_list_scrollbar(buffer, track, metrics, &config.palette);
    }
}

pub(super) fn agent_rows(
    snapshot: &ClientShellSnapshot,
    config: &ClientShellConfig,
    machine: Option<&str>,
) -> Vec<AgentRow> {
    visible_agent_pane_ids(snapshot, config)
        .into_iter()
        .filter_map(|pane_id| agent_row(snapshot, &pane_id, config, machine))
        .collect()
}

pub(super) fn agent_row(
    snapshot: &ClientShellSnapshot,
    pane_id: &str,
    config: &ClientShellConfig,
    machine: Option<&str>,
) -> Option<AgentRow> {
    let agent = snapshot
        .agents
        .iter()
        .find(|agent| agent.pane_id == pane_id)?;
    let workspace = snapshot
        .workspaces
        .iter()
        .find(|workspace| workspace.workspace_id == agent.workspace_id)?;
    let tab = snapshot.tabs.iter().find(|tab| tab.tab_id == agent.tab_id);
    let pane = snapshot
        .panes
        .iter()
        .find(|pane| pane.pane_id == agent.pane_id);
    let tab_count = snapshot
        .tabs
        .iter()
        .filter(|candidate| candidate.workspace_id == agent.workspace_id)
        .count();
    let tab_label = tab
        .filter(|tab| tab_count > 1 || tab.custom_label)
        .map(|tab| tab.label.as_str());
    let agent_label = agent
        .display_agent
        .as_deref()
        .or(agent.name.as_deref())
        .or(agent.agent.as_deref())
        .or(agent.title.as_deref());
    let labels = agent
        .state_labels
        .iter()
        .cloned()
        .collect::<HashMap<_, _>>();
    let tokens = agent.tokens.iter().cloned().collect::<HashMap<_, _>>();
    let state_text = labels
        .get(status_text(agent.agent_status))
        .map(String::as_str)
        .unwrap_or_else(|| sidebar_status_text(agent.agent_status));
    let canonical_agent = agent
        .agent
        .as_deref()
        .and_then(crate::detect::parse_agent_label);
    let rows = crate::ui::sidebar_agent_rows(
        &config.agents,
        crate::ui::AgentTokenContext {
            machine,
            workspace: &workspace.label,
            tab: tab_label,
            pane: agent
                .title
                .as_deref()
                .or_else(|| pane.and_then(|pane| pane.label.as_deref())),
            agent_label,
            terminal_title: agent.terminal_title.as_deref(),
            terminal_title_stripped: agent.terminal_title_stripped.as_deref(),
            canonical_agent,
            tokens: &tokens,
        },
        state_text,
    );
    Some(AgentRow {
        pane_id: agent.pane_id.clone(),
        status: agent.agent_status,
        focused: agent.focused,
        rows,
    })
}

pub(super) fn render_agent_row(
    buffer: &mut Buffer,
    rect: Rect,
    row: &AgentRow,
    config: &ClientShellConfig,
) {
    let palette = &config.palette;
    let row_style = if row.focused {
        Style::default().bg(palette.active_row_bg)
    } else {
        Style::default()
    };
    let name_style = if row.focused {
        Style::default()
            .fg(palette.text)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default()
            .fg(palette.subtext0)
            .add_modifier(Modifier::BOLD)
    };
    let status_style = Style::default().fg(status_color(row.status, palette));
    let secondary = Style::default().fg(palette.overlay0);
    let icon = (
        status_icon(row.status, config.status_indicators),
        Style::default().fg(status_color(row.status, palette)),
    );
    let rows = if row.rows.is_empty() {
        vec![vec![crate::ui::ResolvedToken {
            kind: crate::ui::ResolvedTokenKind::StateIcon,
            style: Default::default(),
        }]]
    } else {
        row.rows.clone()
    };
    for (index, tokens) in rows.iter().take(rect.height as usize).enumerate() {
        let indent = if index == 0 { 1 } else { 3 };
        let mut spans = vec![ratatui::text::Span::raw(" ".repeat(indent))];
        spans.extend(crate::ui::resolved_token_spans(
            tokens,
            icon,
            status_style,
            name_style,
            secondary,
            secondary,
            palette,
            rect.width.saturating_sub(indent as u16) as usize,
        ));
        Paragraph::new(Line::from(spans)).style(row_style).render(
            Rect::new(rect.x, rect.y + index as u16, rect.width, 1),
            buffer,
        );
    }
}

fn put_text(buffer: &mut Buffer, x: u16, y: u16, width: u16, text: &str, style: Style) {
    for (offset, character) in text.chars().take(width as usize).enumerate() {
        if let Some(cell) = buffer.cell_mut((x + offset as u16, y)) {
            cell.set_char(character).set_style(style);
        }
    }
}

fn display_width(text: &str) -> usize {
    unicode_width::UnicodeWidthStr::width(text)
}

fn sidebar_status_text(status: crate::api::schema::AgentStatus) -> &'static str {
    use crate::api::schema::AgentStatus;
    // Presentation only: status keys, priority and detector authority stay stable.
    #[cfg(feature = "watchtower")]
    match status {
        AgentStatus::Blocked => "Needs your input",
        AgentStatus::Done => "Result ready",
        AgentStatus::Working => "Working",
        AgentStatus::Idle => "Ready for task",
        AgentStatus::Unknown => "Status unknown",
    }
    #[cfg(not(feature = "watchtower"))]
    match status {
        AgentStatus::Blocked => "blocked",
        AgentStatus::Done => "done",
        AgentStatus::Working => "working",
        AgentStatus::Idle | AgentStatus::Unknown => "idle",
    }
}

#[cfg(test)]
mod role_tests {
    use super::*;
    use crate::api::schema::AgentStatus;
    use crate::config::AgentPanelSortConfig;
    use crate::protocol::ClientShellAgent;

    fn agent(pane_id: &str, role: Option<&str>, status: AgentStatus) -> ClientShellAgent {
        ClientShellAgent {
            pane_id: pane_id.into(),
            workspace_id: "ws_1".into(),
            tab_id: "tab_1".into(),
            name: Some("controller-reviewer-worker".into()),
            display_agent: None,
            agent: Some("codex".into()),
            title: None,
            terminal_title: None,
            terminal_title_stripped: None,
            agent_status: status,
            state_change_seq: 1,
            state_labels: Vec::new(),
            tokens: role
                .map(|role| vec![("team_role".into(), role.into())])
                .unwrap_or_default(),
            focused: false,
        }
    }

    #[cfg(feature = "watchtower")]
    #[test]
    fn watchtower_agent_states_are_clear_without_changing_status_keys_or_priority() {
        for (status, label, key, priority) in [
            (AgentStatus::Blocked, "Needs your input", "blocked", 4),
            (AgentStatus::Done, "Result ready", "done", 3),
            (AgentStatus::Working, "Working", "working", 2),
            (AgentStatus::Idle, "Ready for task", "idle", 1),
            (AgentStatus::Unknown, "Status unknown", "unknown", 0),
        ] {
            assert_eq!(sidebar_status_text(status), label);
            assert_eq!(status_text(status), key);
            assert_eq!(status_priority(status), priority);
        }
    }

    #[cfg(not(feature = "watchtower"))]
    #[test]
    fn upstream_agent_state_labels_remain_unchanged() {
        for (status, label) in [
            (AgentStatus::Blocked, "blocked"),
            (AgentStatus::Done, "done"),
            (AgentStatus::Working, "working"),
            (AgentStatus::Idle, "idle"),
            (AgentStatus::Unknown, "idle"),
        ] {
            assert_eq!(sidebar_status_text(status), label);
        }
    }

    #[cfg(feature = "watchtower")]
    #[test]
    fn friendly_agent_states_preserve_custom_labels_and_role_tokens() {
        let mut snapshot = super::super::tests::snapshot();
        snapshot.agents = vec![agent("pane_1", Some("⚙  WORKER"), AgentStatus::Blocked)];
        snapshot.agents[0].state_labels = vec![("blocked".into(), "Approval needed".into())];
        let mut config = Config::default();
        config.ui.sidebar.agents.rows = vec![vec![
            crate::config::AgentSidebarToken::Custom("team_role".into()),
            crate::config::AgentSidebarToken::StateText,
        ]];
        let config = ClientShellConfig::from_config(&config);
        let row = agent_row(&snapshot, "pane_1", &config, None).unwrap();
        assert_eq!(row.status, AgentStatus::Blocked);
        let rect = Rect::new(0, 0, 64, 2);
        let mut buffer = Buffer::empty(rect);
        render_agent_row(&mut buffer, rect, &row, &config);
        let text: String = (0..64).map(|x| buffer[(x, 0)].symbol()).collect();
        assert!(text.contains("WORKER"), "{text}");
        assert!(text.contains("⚙"), "{text}");
        assert!(text.contains("Approval needed"), "{text}");
        assert!(!text.contains("Needs your input"), "{text}");
    }

    #[test]
    fn role_rank_accepts_canonical_badges_and_exact_plain_aliases() {
        for (value, expected) in [
            ("👤  CONTROL", 0),
            (" ControlLER ", 0),
            ("control", 0),
            ("⚙  WORKER", 1),
            ("⚙️  WORKER", 1),
            ("worker", 1),
            ("🔎  REVIEW", 2),
            ("Reviewer", 2),
            ("review", 2),
            ("", 3),
            ("lead", 3),
            ("review pending", 3),
            ("unassigned", 3),
        ] {
            assert_eq!(
                agent_role_rank(&[("team_role".into(), value.into())]),
                expected,
                "{value}"
            );
        }
        assert_eq!(agent_role_rank(&[]), 3);
        assert_eq!(agent_role_rank(&[("role".into(), "control".into())]), 3);
    }

    #[test]
    fn role_order_is_stable_and_does_not_infer_names_or_prioritize_status() {
        let mut snapshot = super::super::tests::snapshot();
        snapshot.agents = vec![
            agent("unknown", None, AgentStatus::Blocked),
            agent("worker-1", Some("worker"), AgentStatus::Idle),
            agent("reviewer", Some("reviewer"), AgentStatus::Done),
            agent("controller-1", Some("control"), AgentStatus::Idle),
            agent("worker-2", Some("⚙  WORKER"), AgentStatus::Blocked),
            agent("controller-2", Some("👤  CONTROL"), AgentStatus::Working),
        ];
        // Different workspaces retain the snapshot's order within each role.
        snapshot.agents[4].workspace_id = "ws_2".into();
        assert_eq!(
            ordered_agent_pane_ids(&snapshot, AgentPanelSortConfig::Role),
            [
                "controller-1",
                "controller-2",
                "worker-1",
                "worker-2",
                "reviewer",
                "unknown"
            ]
        );
        assert_eq!(
            ordered_agent_pane_ids(&snapshot, AgentPanelSortConfig::Spaces),
            [
                "unknown",
                "worker-1",
                "reviewer",
                "controller-1",
                "worker-2",
                "controller-2"
            ]
        );
        assert_eq!(
            ordered_agent_pane_ids(&snapshot, AgentPanelSortConfig::Priority)[0],
            "unknown"
        );
        snapshot.agents[0].tokens = vec![("team_role".into(), "control".into())];
        assert_eq!(
            ordered_agent_pane_ids(&snapshot, AgentPanelSortConfig::Role)[0],
            "unknown"
        );
    }

    #[test]
    fn role_order_respects_explicit_agent_view_projection() {
        let mut snapshot = super::super::tests::snapshot();
        snapshot.agents = vec![
            agent("controller", Some("control"), AgentStatus::Idle),
            agent("reviewer", Some("review"), AgentStatus::Idle),
        ];
        snapshot.agent_view_label = Some("custom".into());
        snapshot.agent_order = vec!["reviewer".into(), "missing".into(), "controller".into()];
        assert_eq!(
            ordered_agent_pane_ids(&snapshot, AgentPanelSortConfig::Role),
            ["reviewer", "controller"]
        );
    }

    #[test]
    fn role_header_has_native_toggle_unless_custom_view_is_active() {
        let mut config = ClientShellConfig::from_config(&Config::default());
        config.agent_panel_sort = AgentPanelSortConfig::Role;
        config.mouse_capture = true;
        let area = Rect::new(0, 0, 30, 3);
        let mut buffer = Buffer::empty(area);
        let mut hits = ShellHitMap::default();
        assert!(render_agent_panel_header(
            &mut buffer,
            area,
            None,
            &config,
            &mut hits
        ));
        assert_eq!(hits.agent_sort_toggle, Rect::new(24, 1, 6, 1));
        let text: String = (24..30).map(|x| buffer[(x, 1)].symbol()).collect();
        assert_eq!(text, "role ▾");
        assert!(render_agent_panel_header(
            &mut buffer,
            area,
            Some("custom"),
            &config,
            &mut hits
        ));
        #[cfg(feature = "watchtower")]
        assert!(!hits.agent_sort_toggle.is_empty());
        #[cfg(not(feature = "watchtower"))]
        assert_eq!(hits.agent_sort_toggle, Rect::default());
    }

    #[test]
    fn narrow_agent_header_keeps_a_dropdown_arrow_without_overlapping_title() {
        let mut config = ClientShellConfig::from_config(&Config::default());
        config.agent_panel_sort = AgentPanelSortConfig::Priority;
        config.mouse_capture = true;
        let area = Rect::new(0, 0, 12, 3);
        let mut buffer = Buffer::empty(area);
        let mut hits = ShellHitMap::default();
        render_agent_panel_header(&mut buffer, area, None, &config, &mut hits);
        assert_eq!(hits.agent_sort_toggle, Rect::new(11, 1, 1, 1));
        assert_eq!(buffer[(11, 1)].symbol(), "▾");
        let title: String = (0..7).map(|x| buffer[(x, 1)].symbol()).collect();
        assert_eq!(title, " agents");
        config.mouse_capture = false;
        render_agent_panel_header(&mut buffer, area, None, &config, &mut hits);
        assert!(hits.agent_sort_toggle.is_empty());
    }

    #[test]
    fn name_and_recent_orders_use_visible_names_and_stable_ties() {
        let mut snapshot = super::super::tests::snapshot();
        snapshot.agents = vec![
            agent("z", None, AgentStatus::Blocked),
            agent("a", None, AgentStatus::Idle),
            agent("a-tie", None, AgentStatus::Working),
        ];
        snapshot.agents[0].name = Some("Zulu".into());
        snapshot.agents[0].state_change_seq = 2;
        snapshot.agents[1].name = Some("alpha".into());
        snapshot.agents[1].state_change_seq = 5;
        snapshot.agents[2].display_agent = Some("ALPHA".into());
        snapshot.agents[2].state_change_seq = 5;
        for sort in [AgentPanelSortConfig::Name, AgentPanelSortConfig::Recent] {
            assert_eq!(ordered_agent_pane_ids(&snapshot, sort), ["a", "a-tie", "z"]);
        }
        snapshot.agent_view_label = Some("explicit".into());
        snapshot.agent_order = vec!["z".into(), "a-tie".into()];
        for sort in [AgentPanelSortConfig::Name, AgentPanelSortConfig::Recent] {
            assert_eq!(ordered_agent_pane_ids(&snapshot, sort), ["z", "a-tie"]);
        }
    }
}
