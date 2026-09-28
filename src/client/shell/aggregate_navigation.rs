// Watchtower modifications: independent preview identity, isolation or role presentation.
//! Endpoint-qualified rows shared by aggregate navigation surfaces.

use super::*;
use crate::protocol::ClientShellAgent;

#[derive(Clone, Copy)]
pub(super) struct CachedEndpointSnapshot<'a> {
    pub(super) endpoint_index: usize,
    pub(super) endpoint_id: &'a ClientEndpointId,
    pub(super) label: &'a str,
    pub(super) status: ClientEndpointStatus,
    pub(super) snapshot: &'a ClientShellSnapshot,
    pub(super) agent_recency: &'a HashMap<String, u64>,
    pub(super) agent_presentation: &'a super::endpoint_agent_state::EndpointAgentPresentation,
}

impl CachedEndpointSnapshot<'_> {
    pub(super) fn stale(self) -> bool {
        self.status != ClientEndpointStatus::Online
    }
}

pub(super) fn cached_endpoint_snapshots(
    endpoints: &[ClientShellEndpoint],
) -> impl Iterator<Item = CachedEndpointSnapshot<'_>> {
    endpoints
        .iter()
        .enumerate()
        .filter_map(|(endpoint_index, endpoint)| {
            endpoint
                .snapshot
                .as_deref()
                .map(|snapshot| CachedEndpointSnapshot {
                    endpoint_index,
                    endpoint_id: &endpoint.endpoint_id,
                    label: &endpoint.label,
                    status: endpoint.status,
                    snapshot,
                    agent_recency: &endpoint.agent_recency,
                    agent_presentation: &endpoint.agent_presentation,
                })
        })
}

pub(super) struct AggregateAgentRow<'a> {
    pub(super) endpoint: CachedEndpointSnapshot<'a>,
    pub(super) agent: &'a ClientShellAgent,
    pub(super) recency: u64,
}

pub(super) struct AggregateAgentTarget {
    pub(super) endpoint_id: ClientEndpointId,
    pub(super) pane_id: String,
}

pub(super) fn aggregate_agent_rows<'a>(
    endpoints: &'a [ClientShellEndpoint],
    active_endpoint_id: &ClientEndpointId,
    sort: crate::config::AgentPanelSortConfig,
) -> Vec<AggregateAgentRow<'a>> {
    let active_index = endpoints
        .iter()
        .position(|endpoint| &endpoint.endpoint_id == active_endpoint_id);
    let active_view = active_index.and_then(|index| {
        let endpoint = &endpoints[index];
        if !endpoint.agent_view_projection_supported {
            return None;
        }
        match ClientShellState::endpoint_agent_view(endpoint) {
            Some(Ok(view)) => Some(Ok(view.as_ref())),
            Some(Err(())) => Some(Err(())),
            None if endpoint
                .snapshot
                .as_deref()
                .is_some_and(|snapshot| snapshot.agent_view_label.is_none()) =>
            {
                Some(Ok(None))
            }
            None => Some(Err(())),
        }
    });

    if let Some(Ok(view)) = active_view {
        let mut rows = cached_endpoint_snapshots(endpoints)
            .flat_map(|endpoint| {
                endpoint
                    .snapshot
                    .agents
                    .iter()
                    .map(move |agent| AggregateAgentRow {
                        recency: endpoint
                            .agent_recency
                            .get(&agent.pane_id)
                            .copied()
                            .unwrap_or_default(),
                        endpoint,
                        agent,
                    })
            })
            .collect::<Vec<_>>();
        if let Some(view) = view {
            let context = active_index
                .and_then(|index| endpoints[index].snapshot.as_deref())
                .map(|snapshot| crate::agent_view_eval::AgentViewContext {
                    scope: active_index.unwrap_or_default(),
                    workspace_id: snapshot.focused_workspace_id.clone(),
                    tab_id: snapshot.focused_tab_id.clone(),
                });
            if let (Some(context), Some(filter)) = (context.as_ref(), view.filter.as_ref()) {
                rows.retain(|row| {
                    crate::agent_view_eval::matches_filter(
                        context,
                        &ClientAgentViewEntry::new(row),
                        filter,
                    )
                });
            }
            if !view.sort.is_empty() {
                rows.sort_by(|left, right| {
                    crate::agent_view_eval::compare_entries(
                        &ClientAgentViewEntry::new(left),
                        &ClientAgentViewEntry::new(right),
                        &view.sort,
                    )
                });
                return rows;
            }
        }
        sort_aggregate_rows(&mut rows, sort);
        return rows;
    }

    let mut rows = cached_endpoint_snapshots(endpoints)
        .flat_map(|endpoint| {
            // Server sequence numbers are local to each endpoint. Keep their
            // input order intact before applying client-observed recency below.
            let endpoint_sort = if sort == crate::config::AgentPanelSortConfig::Recent {
                crate::config::AgentPanelSortConfig::Spaces
            } else {
                sort
            };
            super::agent_sidebar::ordered_agent_pane_ids(endpoint.snapshot, endpoint_sort)
                .into_iter()
                .filter_map(move |pane_id| {
                    let agent = endpoint
                        .snapshot
                        .agents
                        .iter()
                        .find(|agent| agent.pane_id == pane_id)?;
                    Some(AggregateAgentRow {
                        recency: endpoint
                            .agent_recency
                            .get(&pane_id)
                            .copied()
                            .unwrap_or_default(),
                        endpoint,
                        agent,
                    })
                })
        })
        .collect::<Vec<_>>();
    sort_aggregate_rows(&mut rows, sort);
    rows
}

fn sort_aggregate_rows(
    rows: &mut [AggregateAgentRow<'_>],
    sort: crate::config::AgentPanelSortConfig,
) {
    if sort == crate::config::AgentPanelSortConfig::Priority {
        rows.sort_by_key(|row| {
            (
                row.endpoint.stale(),
                std::cmp::Reverse(status_priority(row.agent.agent_status)),
                std::cmp::Reverse(row.recency),
            )
        });
    } else if sort == crate::config::AgentPanelSortConfig::Role {
        rows.sort_by_key(|row| super::agent_sidebar::agent_role_rank(&row.agent.tokens));
    } else if sort == crate::config::AgentPanelSortConfig::Recent {
        rows.sort_by_key(|row| std::cmp::Reverse(row.recency));
    } else if sort == crate::config::AgentPanelSortConfig::Name {
        rows.sort_by_cached_key(|row| super::agent_sidebar::agent_sort_name(row.agent));
    }
}

/// Workspace identifiers are endpoint-local. A workspace scope must never
/// match an identically named workspace on another machine.
pub(super) fn visible_aggregate_agent_rows<'a>(
    endpoints: &'a [ClientShellEndpoint],
    active_endpoint_id: &ClientEndpointId,
    config: &ClientShellConfig,
) -> Vec<AggregateAgentRow<'a>> {
    let mut rows = aggregate_agent_rows(endpoints, active_endpoint_id, config.agent_panel_sort);
    if config.agent_current_workspace_only {
        let workspace_id = endpoints
            .iter()
            .find(|endpoint| &endpoint.endpoint_id == active_endpoint_id)
            .and_then(|endpoint| endpoint.snapshot.as_deref())
            .and_then(|snapshot| snapshot.focused_workspace_id.as_deref());
        rows.retain(|row| {
            row.endpoint.endpoint_id == active_endpoint_id
                && Some(row.agent.workspace_id.as_str()) == workspace_id
        });
    }
    rows
}

struct ClientAgentViewEntry<'a> {
    endpoint_index: usize,
    snapshot: &'a ClientShellSnapshot,
    agent: &'a ClientShellAgent,
    seen: bool,
}

impl<'a> ClientAgentViewEntry<'a> {
    fn new(row: &AggregateAgentRow<'a>) -> Self {
        Self {
            endpoint_index: row.endpoint.endpoint_index,
            snapshot: row.endpoint.snapshot,
            agent: row.agent,
            seen: row.endpoint.agent_presentation.seen(row.agent),
        }
    }
}

impl crate::agent_view_eval::AgentViewEntry for ClientAgentViewEntry<'_> {
    fn scope(&self) -> usize {
        self.endpoint_index
    }

    fn status(&self) -> &'static str {
        status_text(self.agent.agent_status)
    }

    fn workspace_id(&self) -> Option<std::borrow::Cow<'_, str>> {
        Some(std::borrow::Cow::Borrowed(&self.agent.workspace_id))
    }

    fn tab_id(&self) -> Option<std::borrow::Cow<'_, str>> {
        Some(std::borrow::Cow::Borrowed(&self.agent.tab_id))
    }

    fn pane_id(&self) -> Option<std::borrow::Cow<'_, str>> {
        Some(std::borrow::Cow::Borrowed(&self.agent.pane_id))
    }

    fn agent(&self) -> Option<&str> {
        self.agent.agent.as_deref()
    }

    fn seen(&self) -> bool {
        self.seen
    }

    fn state_change_seq(&self) -> Option<u64> {
        Some(self.agent.state_change_seq)
    }

    fn token(&self, token: &str) -> Option<&str> {
        self.agent
            .tokens
            .iter()
            .find(|(name, _)| name == token)
            .map(|(_, value)| value.as_str())
    }

    fn workspace_order(&self) -> Option<u64> {
        self.snapshot
            .workspaces
            .iter()
            .position(|workspace| workspace.workspace_id == self.agent.workspace_id)
            .map(|index| index as u64)
    }

    fn tab_order(&self) -> Option<u64> {
        self.snapshot
            .tabs
            .iter()
            .find(|tab| tab.tab_id == self.agent.tab_id)
            .map(|tab| tab.number as u64)
    }

    fn pane_order(&self) -> Option<u64> {
        let suffix = self
            .agent
            .pane_id
            .strip_prefix(&self.agent.workspace_id)?
            .strip_prefix(":p")?;
        crate::workspace::decode_public_number(suffix).map(|number| number as u64)
    }

    fn attention(&self) -> u64 {
        u64::from(status_priority(self.agent.agent_status))
    }
}

pub(super) fn online_agent_targets(
    endpoints: &[ClientShellEndpoint],
    active_endpoint_id: &ClientEndpointId,
    sort: crate::config::AgentPanelSortConfig,
) -> Vec<AggregateAgentTarget> {
    aggregate_agent_rows(endpoints, active_endpoint_id, sort)
        .into_iter()
        .filter(|row| !row.endpoint.stale())
        .map(|row| AggregateAgentTarget {
            endpoint_id: row.endpoint.endpoint_id.clone(),
            pane_id: row.agent.pane_id.clone(),
        })
        .collect()
}

pub(super) fn visible_online_agent_targets(
    endpoints: &[ClientShellEndpoint],
    active_endpoint_id: &ClientEndpointId,
    config: &ClientShellConfig,
) -> Vec<AggregateAgentTarget> {
    if !config.agent_current_workspace_only {
        return online_agent_targets(endpoints, active_endpoint_id, config.agent_panel_sort);
    }
    visible_aggregate_agent_rows(endpoints, active_endpoint_id, config)
        .into_iter()
        .filter(|row| !row.endpoint.stale())
        .map(|row| AggregateAgentTarget {
            endpoint_id: row.endpoint.endpoint_id.clone(),
            pane_id: row.agent.pane_id.clone(),
        })
        .collect()
}

pub(super) fn navigator_rows(
    endpoints: &[ClientShellEndpoint],
    active_endpoint_id: &ClientEndpointId,
    navigator: &ClientNavigatorOverlay,
) -> Vec<ClientNavigatorRow> {
    let query = navigator.query.trim().to_lowercase();
    let filter = |status| match navigator.filter {
        Some(ClientNavigatorFilter::Blocked) => status == crate::api::schema::AgentStatus::Blocked,
        Some(ClientNavigatorFilter::Working) => status == crate::api::schema::AgentStatus::Working,
        Some(ClientNavigatorFilter::Idle) => status == crate::api::schema::AgentStatus::Idle,
        Some(ClientNavigatorFilter::Done) => status == crate::api::schema::AgentStatus::Done,
        None => true,
    };
    let text = |value: &str| query.is_empty() || value.to_lowercase().contains(&query);
    let filtering = navigator.filter.is_some() || !query.is_empty();
    let federated = endpoints.len() > 1;
    let depth_offset = u8::from(federated);
    let mut rows = Vec::new();

    for endpoint in endpoints {
        let stale = endpoint.status != ClientEndpointStatus::Online;
        let endpoint_query_matches = !query.is_empty() && text(&endpoint.label);
        let mut endpoint_rows = Vec::new();
        if let Some(snapshot) = endpoint.snapshot.as_deref() {
            for workspace in &snapshot.workspaces {
                let workspace_meta = workspace.branch.clone().unwrap_or_default();
                let mut children = Vec::new();
                for tab in snapshot
                    .tabs
                    .iter()
                    .filter(|tab| tab.workspace_id == workspace.workspace_id)
                {
                    let mut panes = Vec::new();
                    for (index, pane) in snapshot
                        .panes
                        .iter()
                        .filter(|pane| pane.tab_id == tab.tab_id)
                        .enumerate()
                    {
                        let agent = snapshot
                            .agents
                            .iter()
                            .find(|agent| agent.pane_id == pane.pane_id);
                        let status = agent
                            .map_or(crate::api::schema::AgentStatus::Unknown, |agent| {
                                agent.agent_status
                            });
                        let label = pane
                            .label
                            .clone()
                            .or_else(|| agent.and_then(|agent| agent.name.clone()))
                            .or_else(|| agent.and_then(|agent| agent.display_agent.clone()))
                            .or_else(|| agent.and_then(|agent| agent.title.clone()))
                            .unwrap_or_else(|| format!("pane {}", index + 1));
                        let meta = pane
                            .foreground_cwd
                            .clone()
                            .or_else(|| pane.cwd.clone())
                            .unwrap_or_default();
                        if !filtering
                            || filter(status)
                                && (endpoint_query_matches || text(&label) || text(&meta))
                        {
                            panes.push(ClientNavigatorRow {
                                depth: 2 + depth_offset,
                                label,
                                meta,
                                status: Some(status),
                                stale,
                                current: endpoint.endpoint_id == *active_endpoint_id
                                    && snapshot.focused_pane_id.as_deref() == Some(&pane.pane_id),
                                target: ClientNavigatorTarget::Pane {
                                    endpoint_id: endpoint.endpoint_id.clone(),
                                    pane_id: pane.pane_id.clone(),
                                },
                            });
                        }
                    }
                    if !filtering
                        || filter(tab.agent_status) && (endpoint_query_matches || text(&tab.label))
                        || !panes.is_empty()
                    {
                        children.push(ClientNavigatorRow {
                            depth: 1 + depth_offset,
                            label: tab.label.clone(),
                            meta: format!(
                                "{} panes",
                                snapshot
                                    .panes
                                    .iter()
                                    .filter(|pane| pane.tab_id == tab.tab_id)
                                    .count()
                            ),
                            status: None,
                            stale,
                            current: false,
                            target: ClientNavigatorTarget::Tab {
                                endpoint_id: endpoint.endpoint_id.clone(),
                                tab_id: tab.tab_id.clone(),
                            },
                        });
                        children.extend(panes);
                    }
                }
                let workspace_matches = filter(workspace.agent_status)
                    && (endpoint_query_matches || text(&workspace.label) || text(&workspace_meta));
                if !filtering || workspace_matches || !children.is_empty() {
                    let key = (endpoint.endpoint_id.clone(), workspace.workspace_id.clone());
                    endpoint_rows.push(ClientNavigatorRow {
                        depth: depth_offset,
                        label: workspace.label.clone(),
                        meta: workspace_meta,
                        status: None,
                        stale,
                        current: false,
                        target: ClientNavigatorTarget::Workspace {
                            endpoint_id: endpoint.endpoint_id.clone(),
                            workspace_id: workspace.workspace_id.clone(),
                        },
                    });
                    if navigator.expanded_workspaces.contains(&key) || filtering {
                        endpoint_rows.extend(children);
                    }
                }
            }
        }
        if !filtering || endpoint_query_matches || !endpoint_rows.is_empty() {
            if federated {
                rows.push(ClientNavigatorRow {
                    depth: 0,
                    label: endpoint.label.to_owned(),
                    meta: String::new(),
                    status: None,
                    stale,
                    current: false,
                    target: ClientNavigatorTarget::Machine {
                        endpoint_id: endpoint.endpoint_id.clone(),
                    },
                });
            }
            rows.extend(endpoint_rows);
        }
    }
    rows
}

pub(super) fn navigator_selected_index(
    rows: &[ClientNavigatorRow],
    navigator: &ClientNavigatorOverlay,
) -> Option<usize> {
    match navigator.selected.as_ref() {
        Some(target) => rows.iter().position(|row| row.target == *target),
        None => (!rows.is_empty()).then_some(0),
    }
}

pub(super) fn selected_navigator_target(
    rows: &[ClientNavigatorRow],
    navigator: &ClientNavigatorOverlay,
) -> Option<ClientNavigatorTarget> {
    navigator_selected_index(rows, navigator).map(|index| rows[index].target.clone())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::schema::{
        AgentStatus, AgentViewBuiltinSortField, AgentViewSetParams, AgentViewSort,
        AgentViewSortField, AgentViewSortOrder,
    };
    use crate::client::endpoint::{ProfileId, SavedSshEndpoint};
    use crate::config::{AgentPanelSortConfig, Config};

    fn role_agent(
        pane_id: &str,
        workspace_id: &str,
        role: &str,
        state_change_seq: u64,
    ) -> ClientShellAgent {
        ClientShellAgent {
            pane_id: pane_id.into(),
            workspace_id: workspace_id.into(),
            tab_id: if workspace_id == "ws_1" {
                "tab_1"
            } else {
                "tab_2"
            }
            .into(),
            name: Some(role.into()),
            display_agent: None,
            agent: Some("codex".into()),
            title: None,
            terminal_title: None,
            terminal_title_stripped: None,
            agent_status: AgentStatus::Idle,
            state_change_seq,
            state_labels: Vec::new(),
            tokens: vec![("team_role".into(), role.into())],
            focused: false,
        }
    }

    fn role_snapshot(agents: Vec<ClientShellAgent>) -> ClientShellSnapshot {
        let mut snapshot = super::super::tests::snapshot();
        let mut second_workspace = snapshot.workspaces[0].clone();
        second_workspace.workspace_id = "ws_2".into();
        second_workspace.active_tab_id = "tab_2".into();
        second_workspace.number = 2;
        second_workspace.focused = false;
        snapshot.workspaces.push(second_workspace);
        let mut second_tab = snapshot.tabs[0].clone();
        second_tab.tab_id = "tab_2".into();
        second_tab.workspace_id = "ws_2".into();
        second_tab.focused = false;
        snapshot.tabs.push(second_tab);
        snapshot.panes = agents
            .iter()
            .map(|agent| crate::protocol::ClientShellPane {
                pane_id: agent.pane_id.clone(),
                workspace_id: agent.workspace_id.clone(),
                tab_id: agent.tab_id.clone(),
                focused: false,
                ..snapshot.panes[0].clone()
            })
            .collect();
        snapshot.agents = agents;
        snapshot
    }

    fn role_endpoints(
        local_agents: Vec<ClientShellAgent>,
        remote_agents: Vec<ClientShellAgent>,
    ) -> (ClientShellState, ClientEndpointId) {
        let mut state = ClientShellState::new(ClientShellConfig::from_config(&Config::default()));
        let profile = SavedSshEndpoint {
            id: ProfileId::parse("0123456789abcdef0123456789abcdef").unwrap(),
            label: "Build".into(),
            target: "dev@build.example".into(),
            session: "agents".into(),
            enabled: true,
        };
        let remote_id = ClientEndpointId::Ssh(profile.id.clone());
        state.set_endpoint_catalog(&[profile]);
        state.set_snapshot(Box::new(role_snapshot(local_agents)));
        let mut remote = role_snapshot(remote_agents);
        remote.boot_id = "remote-boot".into();
        state.set_endpoint_snapshot(&remote_id, Box::new(remote));
        state.set_endpoint_status(&ClientEndpointId::Local, ClientEndpointStatus::Online);
        state.set_endpoint_status(&remote_id, ClientEndpointStatus::Online);
        (state, remote_id)
    }

    #[test]
    fn aggregate_role_sort_is_role_first_and_preserves_endpoint_and_workspace_ties() {
        let (state, remote_id) = role_endpoints(
            vec![
                role_agent("local-worker-2", "ws_2", "WORKER", 1),
                role_agent("local-review", "ws_1", "REVIEW", 90),
                role_agent("local-control", "ws_1", "CONTROL", 2),
                role_agent("local-worker-1", "ws_1", "WORKER", 100),
            ],
            vec![
                role_agent("remote-worker-2", "ws_2", "WORKER", 200),
                role_agent("remote-review", "ws_1", "REVIEW", 300),
                role_agent("remote-control", "ws_2", "CONTROL", 3),
                role_agent("remote-worker-1", "ws_1", "WORKER", 400),
            ],
        );
        let rows = aggregate_agent_rows(
            &state.endpoints,
            &ClientEndpointId::Local,
            AgentPanelSortConfig::Role,
        );
        let identities = rows
            .iter()
            .map(|row| {
                (
                    row.endpoint.endpoint_id.clone(),
                    row.agent.workspace_id.as_str(),
                    row.agent.pane_id.as_str(),
                )
            })
            .collect::<Vec<_>>();
        assert_eq!(
            identities,
            [
                (ClientEndpointId::Local, "ws_1", "local-control"),
                (remote_id.clone(), "ws_2", "remote-control"),
                (ClientEndpointId::Local, "ws_2", "local-worker-2"),
                (ClientEndpointId::Local, "ws_1", "local-worker-1"),
                (remote_id.clone(), "ws_2", "remote-worker-2"),
                (remote_id.clone(), "ws_1", "remote-worker-1"),
                (ClientEndpointId::Local, "ws_1", "local-review"),
                (remote_id, "ws_1", "remote-review"),
            ]
        );
    }

    #[test]
    fn aggregate_role_targets_keep_duplicate_pane_ids_endpoint_qualified() {
        let (state, remote_id) = role_endpoints(
            vec![role_agent("pane_1", "ws_1", "WORKER", 99)],
            vec![role_agent("pane_1", "ws_1", "CONTROL", 1)],
        );
        let rows = aggregate_agent_rows(
            &state.endpoints,
            &ClientEndpointId::Local,
            AgentPanelSortConfig::Role,
        );
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].endpoint.endpoint_id, &remote_id);
        assert_eq!(rows[0].agent.name.as_deref(), Some("CONTROL"));
        assert_eq!(rows[1].endpoint.endpoint_id, &ClientEndpointId::Local);
        assert_eq!(rows[1].agent.name.as_deref(), Some("WORKER"));
        let targets = online_agent_targets(
            &state.endpoints,
            &ClientEndpointId::Local,
            AgentPanelSortConfig::Role,
        )
        .into_iter()
        .map(|target| (target.endpoint_id, target.pane_id))
        .collect::<Vec<_>>();
        assert_eq!(
            targets,
            [
                (remote_id, "pane_1".to_owned()),
                (ClientEndpointId::Local, "pane_1".to_owned()),
            ]
        );
    }

    #[test]
    fn aggregate_role_targets_exclude_offline_rows_without_hiding_cached_roles() {
        let (mut state, remote_id) = role_endpoints(
            vec![role_agent("pane_1", "ws_1", "WORKER", 99)],
            vec![role_agent("pane_1", "ws_1", "CONTROL", 1)],
        );
        for status in [
            ClientEndpointStatus::Connecting,
            ClientEndpointStatus::Reconnecting,
            ClientEndpointStatus::Attention,
            ClientEndpointStatus::Disabled,
        ] {
            state.set_endpoint_status(&remote_id, status);
            let rows = aggregate_agent_rows(
                &state.endpoints,
                &ClientEndpointId::Local,
                AgentPanelSortConfig::Role,
            );
            assert_eq!(rows.len(), 2, "status: {status:?}");
            assert_eq!(rows[0].endpoint.endpoint_id, &remote_id);
            assert!(rows[0].endpoint.stale());
            let targets = online_agent_targets(
                &state.endpoints,
                &ClientEndpointId::Local,
                AgentPanelSortConfig::Role,
            );
            assert_eq!(targets.len(), 1, "status: {status:?}");
            assert_eq!(targets[0].endpoint_id, ClientEndpointId::Local);
            assert_eq!(targets[0].pane_id, "pane_1");
        }
    }

    #[test]
    fn aggregate_recent_uses_client_recency_and_keeps_endpoint_ties_stable() {
        for projection_supported in [true, false] {
            let (mut state, remote_id) = role_endpoints(
                vec![
                    role_agent("pane_1", "ws_1", "WORKER", 1),
                    role_agent("pane_2", "ws_1", "WORKER", 9000),
                ],
                vec![
                    role_agent("pane_1", "ws_1", "WORKER", 2),
                    role_agent("pane_2", "ws_1", "WORKER", 999999),
                ],
            );
            for endpoint in &mut state.endpoints {
                endpoint.agent_view_projection_supported = projection_supported;
                endpoint.agent_recency = HashMap::from([
                    (
                        "pane_1".into(),
                        if endpoint.endpoint_id == remote_id {
                            20
                        } else {
                            10
                        },
                    ),
                    ("pane_2".into(), 10),
                ]);
            }
            let rows = aggregate_agent_rows(
                &state.endpoints,
                &ClientEndpointId::Local,
                AgentPanelSortConfig::Recent,
            );
            let identities = rows
                .iter()
                .map(|row| (row.endpoint.endpoint_id.clone(), row.agent.pane_id.as_str()))
                .collect::<Vec<_>>();
            assert_eq!(
                identities,
                [
                    (remote_id.clone(), "pane_1"),
                    (ClientEndpointId::Local, "pane_1"),
                    (ClientEndpointId::Local, "pane_2"),
                    (remote_id, "pane_2"),
                ],
                "projection_supported: {projection_supported}"
            );
        }
    }

    #[test]
    fn aggregate_name_uses_display_label_priority_and_preserves_case_insensitive_ties() {
        let mut display = role_agent("display", "ws_1", "ZZZ", 9000);
        display.display_agent = Some("Beta".into());
        let named = role_agent("named", "ws_1", "ALPHA", 1);
        let mut title = role_agent("title", "ws_1", "WORKER", 500);
        title.name = None;
        title.agent = None;
        title.title = Some("charlie".into());
        let mut remote_display = role_agent("remote-display", "ws_1", "REVIEW", 90000);
        remote_display.display_agent = Some("alpha".into());
        let mut provider = role_agent("provider", "ws_1", "WORKER", 100000);
        provider.name = None;
        provider.agent = Some("delta".into());
        provider.title = Some("aaa".into());
        let (mut state, remote_id) =
            role_endpoints(vec![display, named, title], vec![remote_display, provider]);
        for projection_supported in [true, false] {
            for endpoint in &mut state.endpoints {
                endpoint.agent_view_projection_supported = projection_supported;
            }
            let rows = aggregate_agent_rows(
                &state.endpoints,
                &ClientEndpointId::Local,
                AgentPanelSortConfig::Name,
            );
            let identities = rows
                .iter()
                .map(|row| (row.endpoint.endpoint_id.clone(), row.agent.pane_id.as_str()))
                .collect::<Vec<_>>();
            assert_eq!(
                identities,
                [
                    (ClientEndpointId::Local, "named"),
                    (remote_id.clone(), "remote-display"),
                    (ClientEndpointId::Local, "display"),
                    (ClientEndpointId::Local, "title"),
                    (remote_id.clone(), "provider"),
                ],
                "projection_supported: {projection_supported}"
            );
        }
    }

    #[test]
    fn aggregate_client_sorts_retain_an_explicit_custom_view_sort() {
        let (mut state, remote_id) = role_endpoints(
            vec![role_agent("pane_1", "ws_1", "CONTROL", 1)],
            vec![role_agent("pane_1", "ws_1", "REVIEW", 99)],
        );
        for endpoint in &mut state.endpoints {
            endpoint.agent_recency.insert(
                "pane_1".into(),
                if endpoint.endpoint_id == ClientEndpointId::Local {
                    100
                } else {
                    1
                },
            );
        }
        let sorts = [
            AgentPanelSortConfig::Role,
            AgentPanelSortConfig::Recent,
            AgentPanelSortConfig::Name,
        ];
        for sort in sorts {
            let rows = aggregate_agent_rows(&state.endpoints, &ClientEndpointId::Local, sort);
            assert_eq!(rows[0].endpoint.endpoint_id, &ClientEndpointId::Local);
        }
        state.set_test_endpoint_agent_view(
            &ClientEndpointId::Local,
            Some(AgentViewSetParams {
                source: "tests.role-sort".into(),
                label: Some("recent".into()),
                filter: None,
                sort: vec![AgentViewSort {
                    field: AgentViewSortField::Builtin(AgentViewBuiltinSortField::StateChangeSeq),
                    order: AgentViewSortOrder::Desc,
                }],
            }),
        );
        for sort in sorts {
            let targets = online_agent_targets(&state.endpoints, &ClientEndpointId::Local, sort)
                .into_iter()
                .map(|target| (target.endpoint_id, target.pane_id))
                .collect::<Vec<_>>();
            assert_eq!(
                targets,
                [
                    (remote_id.clone(), "pane_1".to_owned()),
                    (ClientEndpointId::Local, "pane_1".to_owned()),
                ],
                "sort: {sort:?}"
            );
        }
    }
}
