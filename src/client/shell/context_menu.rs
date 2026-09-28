use super::*;

impl ClientContextMenuOverlay {
    pub(super) fn items(&self) -> Vec<ClientContextMenuItem> {
        use ClientContextMenuAction as Action;

        let item = |label: &str, action| ClientContextMenuItem {
            label: label.to_owned(),
            action,
        };
        let items =
            match &self.target {
                #[cfg(feature = "watchtower")]
                ClientContextMenuTarget::SpacesGrouping { grouped, .. } => vec![
                    item(
                        if *grouped {
                            "  Flat list"
                        } else {
                            "✓ Flat list"
                        },
                        Action::SetSpacesGrouped(false),
                    ),
                    item(
                        if *grouped { "✓ Groups" } else { "  Groups" },
                        Action::SetSpacesGrouped(true),
                    ),
                    item("New group...", Action::NewSpaceGroup),
                ],
                #[cfg(feature = "watchtower")]
                ClientContextMenuTarget::SpaceGroup { .. } => vec![
                    item("Rename group...", Action::RenameSpaceGroup),
                    item("Move up", Action::MoveSpaceGroup(-1)),
                    item("Move down", Action::MoveSpaceGroup(1)),
                    item("Remove group", Action::RemoveSpaceGroup),
                ],
                #[cfg(feature = "watchtower")]
                ClientContextMenuTarget::WorkspaceGroups { groups, .. } => {
                    let mut items = vec![
                        item("Ungrouped", Action::AssignSpaceGroup(None)),
                        item("New group...", Action::NewSpaceGroup),
                    ];
                    items.extend(groups.iter().enumerate().map(|(index, (_, name))| {
                        item(name, Action::AssignSpaceGroup(Some(index)))
                    }));
                    items
                }
                ClientContextMenuTarget::AgentPanel {
                    sort,
                    current_workspace_only,
                    sort_locked,
                } => {
                    use crate::config::AgentPanelSortConfig as Sort;
                    let choices = [
                        (Sort::Spaces, "  By workspace", "✓ By workspace"),
                        (
                            Sort::Priority,
                            "  Needs attention first",
                            "✓ Needs attention first",
                        ),
                        (Sort::Role, "  By role", "✓ By role"),
                        (Sort::Recent, "  Recently changed", "✓ Recently changed"),
                        (Sort::Name, "  Name (A–Z)", "✓ Name (A–Z)"),
                    ]
                    .into_iter()
                    .map(|(value, label, selected)| {
                        item(
                            if value == *sort { selected } else { label },
                            Action::SetAgentSort(value),
                        )
                    })
                    .collect::<Vec<_>>();
                    #[cfg(feature = "watchtower")]
                    {
                        let mut choices = if *sort_locked { Vec::new() } else { choices };
                        choices.push(item(
                            if *current_workspace_only {
                                "✓ Current workspace only"
                            } else {
                                "  Current workspace only"
                            },
                            Action::SetAgentWorkspaceFilter(!current_workspace_only),
                        ));
                        choices
                    }
                    #[cfg(not(feature = "watchtower"))]
                    {
                        let _ = (current_workspace_only, sort_locked);
                        choices
                    }
                }
                ClientContextMenuTarget::Workspace { is_git: false, .. } => {
                    vec![item("Rename", Action::Rename), item("Close", Action::Close)]
                }
                ClientContextMenuTarget::Workspace {
                    is_linked_worktree: false,
                    has_worktree_children: false,
                    ..
                } => vec![
                    item("Rename", Action::Rename),
                    item("Close", Action::Close),
                    item("New worktree", Action::NewWorktree),
                    item("Open worktree...", Action::OpenWorktree),
                ],
                ClientContextMenuTarget::Workspace {
                    is_linked_worktree: true,
                    ..
                } => vec![
                    item("Rename", Action::Rename),
                    item("Close", Action::Close),
                    item("Delete worktree checkout...", Action::RemoveWorktree),
                ],
                ClientContextMenuTarget::Workspace {
                    has_worktree_children: true,
                    collapsed,
                    ..
                } => vec![
                    item("Rename", Action::Rename),
                    item("Close group", Action::Close),
                    item("New worktree", Action::NewWorktree),
                    item("Open worktree...", Action::OpenWorktree),
                    item(
                        if *collapsed { "Expand" } else { "Collapse" },
                        Action::ToggleGroup,
                    ),
                ],
                ClientContextMenuTarget::Tab { .. } => vec![
                    item("New tab", Action::NewTab),
                    item("Rename", Action::Rename),
                    item("Close", Action::Close),
                ],
                ClientContextMenuTarget::Pane {
                    source_pane_id,
                    has_manual_label,
                    right_click_passthrough,
                    ..
                } => {
                    let mut items = vec![item("Rename pane", Action::RenamePane)];
                    if *has_manual_label {
                        items.push(item("Clear pane name", Action::ClearPaneName));
                    }
                    if source_pane_id.is_some() {
                        items.push(item("Swap with focused pane", Action::SwapWithFocusedPane));
                    }
                    items.extend([
                        item("Split right", Action::SplitRight),
                        item("Split down", Action::SplitDown),
                        item("Zoom", Action::Zoom),
                        item(
                            if *right_click_passthrough {
                                "Use Herdr right-click menu"
                            } else {
                                "Send right-clicks to pane"
                            },
                            Action::ToggleRightClickPassthrough,
                        ),
                        item("Close pane", Action::ClosePane),
                    ]);
                    items
                }
            };
        #[cfg(feature = "watchtower")]
        let mut items = items;
        #[cfg(feature = "watchtower")]
        if matches!(self.target, ClientContextMenuTarget::Workspace { .. }) {
            items.push(item("Move to group...", Action::MoveToSpaceGroup));
        }
        items
    }
}

impl ClientShellState {
    #[cfg(feature = "watchtower")]
    pub(super) fn open_spaces_grouping_menu(&mut self, x: u16, y: u16) {
        self.overlay = Some(ClientShellOverlay::ContextMenu(ClientContextMenuOverlay {
            target: ClientContextMenuTarget::SpacesGrouping {
                endpoint_id: self.active_endpoint_id.clone(),
                grouped: self.config.preferences.space_groups.grouped,
            },
            x,
            y,
            highlighted: usize::from(self.config.preferences.space_groups.grouped),
        }));
    }

    #[cfg(feature = "watchtower")]
    pub(super) fn open_space_group_context_menu(
        &mut self,
        endpoint_id: ClientEndpointId,
        group_id: String,
        x: u16,
        y: u16,
    ) {
        if !self
            .endpoints
            .iter()
            .any(|endpoint| endpoint.endpoint_id == endpoint_id)
            || !self
                .config
                .preferences
                .space_groups
                .groups
                .iter()
                .any(|group| group.endpoint == endpoint_id.storage_key() && group.id == group_id)
        {
            return;
        }
        self.overlay = Some(ClientShellOverlay::ContextMenu(ClientContextMenuOverlay {
            target: ClientContextMenuTarget::SpaceGroup {
                endpoint_id,
                group_id,
            },
            x,
            y,
            highlighted: 0,
        }));
    }

    #[cfg(feature = "watchtower")]
    pub(super) fn toggle_space_group(
        &mut self,
        endpoint_id: ClientEndpointId,
        group_id: String,
        outcome: &mut ClientShellInput,
    ) {
        if !self
            .endpoints
            .iter()
            .any(|endpoint| endpoint.endpoint_id == endpoint_id)
        {
            self.space_group_error("This endpoint is no longer available.", outcome);
            return;
        }
        let result = self
            .config
            .preferences
            .space_groups
            .toggle(&endpoint_id.storage_key(), &group_id);
        self.finish_space_group_change(result, outcome);
    }

    #[cfg(feature = "watchtower")]
    pub(super) fn space_workspace_family(
        &self,
        target: &ClientSpaceWorkspaceTarget,
    ) -> Option<Vec<String>> {
        // Runtime IDs can be reused after reconnect. Never assign against a newer session.
        if target.endpoint_id != self.active_endpoint_id
            || target.generation != self.active_snapshot_generation
        {
            return None;
        }
        let snapshot = self.snapshot.as_deref()?;
        if snapshot.boot_id != target.boot_id
            || !snapshot
                .workspaces
                .iter()
                .any(|workspace| workspace.workspace_id == target.workspace_id)
        {
            return None;
        }
        Some(super::space_groups::family_workspace_ids(
            snapshot,
            &target.workspace_id,
        ))
    }

    #[cfg(feature = "watchtower")]
    pub(super) fn space_group_error(
        &mut self,
        message: impl Into<String>,
        outcome: &mut ClientShellInput,
    ) {
        outcome.repaint |= self.push_endpoint_notice(
            ClientEndpointNoticeKind::Rejected,
            "space_group",
            "Group not changed",
            message,
        );
    }

    #[cfg(feature = "watchtower")]
    pub(super) fn finish_space_group_change(
        &mut self,
        result: Result<(), String>,
        outcome: &mut ClientShellInput,
    ) {
        match result {
            Ok(()) => {
                self.persist_chrome_preferences(outcome);
                outcome.repaint = true;
            }
            Err(error) => self.space_group_error(error, outcome),
        }
    }

    #[cfg(feature = "watchtower")]
    pub(super) fn open_new_space_group_overlay(
        &mut self,
        endpoint_id: ClientEndpointId,
        workspace: Option<ClientSpaceWorkspaceTarget>,
    ) {
        self.overlay = Some(ClientShellOverlay::Rename(ClientRenameOverlay {
            title: "new group",
            input: TextEditor::new("", false),
            target: ClientRenameTarget::NewSpaceGroup {
                endpoint_id,
                workspace,
            },
        }));
    }

    pub(super) fn open_agent_sort_menu(&mut self, x: u16, y: u16) {
        // Custom server views own sorting. Workspace scope is client-local and
        // remains available so an active filter can always be disabled.
        let Some(snapshot) = self.snapshot.as_deref() else {
            return;
        };
        let sort_locked = snapshot.agent_view_label.is_some();
        if !cfg!(feature = "watchtower") && sort_locked {
            return;
        }
        let mut menu = ClientContextMenuOverlay {
            target: ClientContextMenuTarget::AgentPanel {
                sort: self.config.agent_panel_sort,
                current_workspace_only: self.config.agent_current_workspace_only,
                sort_locked,
            },
            x,
            y,
            highlighted: 0,
        };
        menu.highlighted = menu
            .items()
            .iter()
            .position(|item| {
                item.action == ClientContextMenuAction::SetAgentSort(self.config.agent_panel_sort)
            })
            .unwrap_or_default();
        self.overlay = Some(ClientShellOverlay::ContextMenu(menu));
    }

    pub(super) fn open_workspace_context_menu(&mut self, workspace_id: String, x: u16, y: u16) {
        let Some(snapshot) = self.snapshot.as_deref() else {
            return;
        };
        let Some(workspace) = snapshot
            .workspaces
            .iter()
            .find(|workspace| workspace.workspace_id == workspace_id)
        else {
            return;
        };
        let worktree = workspace.worktree.as_ref();
        let has_worktree_children = worktree.is_some_and(|worktree| {
            !worktree.is_linked_worktree
                && snapshot
                    .workspaces
                    .iter()
                    .filter(|candidate| {
                        candidate
                            .worktree
                            .as_ref()
                            .is_some_and(|candidate| candidate.key == worktree.key)
                    })
                    .count()
                    >= 2
        });
        let collapsed = worktree.is_some_and(|worktree| {
            self.group_is_collapsed(&self.active_endpoint_id, &worktree.key)
        });
        self.overlay = Some(ClientShellOverlay::ContextMenu(ClientContextMenuOverlay {
            target: ClientContextMenuTarget::Workspace {
                #[cfg(feature = "watchtower")]
                scope: ClientSpaceWorkspaceTarget {
                    endpoint_id: self.active_endpoint_id.clone(),
                    workspace_id: workspace_id.clone(),
                    boot_id: snapshot.boot_id.clone(),
                    generation: self.active_snapshot_generation,
                },
                workspace_id,
                is_git: worktree.is_some() || workspace.branch.is_some(),
                is_linked_worktree: worktree.is_some_and(|worktree| worktree.is_linked_worktree),
                has_worktree_children,
                collapsed,
            },
            x,
            y,
            highlighted: 0,
        }));
    }

    pub(super) fn open_tab_context_menu(&mut self, tab_id: String, x: u16, y: u16) {
        let Some(tab) = self
            .snapshot
            .as_deref()
            .and_then(|snapshot| snapshot.tabs.iter().find(|tab| tab.tab_id == tab_id))
        else {
            return;
        };
        self.overlay = Some(ClientShellOverlay::ContextMenu(ClientContextMenuOverlay {
            target: ClientContextMenuTarget::Tab {
                tab_id,
                workspace_id: tab.workspace_id.clone(),
            },
            x,
            y,
            highlighted: 0,
        }));
    }

    pub(super) fn open_pane_context_menu(&mut self, pane_id: String, x: u16, y: u16) {
        let Some(snapshot) = self.snapshot.as_deref() else {
            return;
        };
        let Some(pane) = snapshot.panes.iter().find(|pane| pane.pane_id == pane_id) else {
            return;
        };
        let source_pane_id = snapshot
            .focused_pane_id
            .clone()
            .filter(|focused| focused != &pane_id);
        self.overlay = Some(ClientShellOverlay::ContextMenu(ClientContextMenuOverlay {
            target: ClientContextMenuTarget::Pane {
                pane_id,
                workspace_id: pane.workspace_id.clone(),
                source_pane_id,
                has_manual_label: pane.label.is_some(),
                right_click_passthrough: pane.right_click_passthrough,
            },
            x,
            y,
            highlighted: 0,
        }));
    }

    pub(super) fn move_context_menu_selection(&mut self, delta: isize) {
        let Some(ClientShellOverlay::ContextMenu(menu)) = self.overlay.as_mut() else {
            return;
        };
        let item_count = menu.items().len();
        if item_count == 0 {
            return;
        }
        menu.highlighted = (menu.highlighted as isize + delta)
            .clamp(0, item_count.saturating_sub(1) as isize) as usize;
    }

    pub(super) fn activate_context_menu_item(
        &mut self,
        index: usize,
        outcome: &mut ClientShellInput,
    ) {
        let Some(ClientShellOverlay::ContextMenu(menu)) = self.overlay.take() else {
            return;
        };
        let Some(action) = menu.items().get(index).map(|item| item.action) else {
            outcome.repaint = true;
            return;
        };
        match menu.target {
            #[cfg(feature = "watchtower")]
            ClientContextMenuTarget::SpacesGrouping { endpoint_id, .. } => match action {
                ClientContextMenuAction::SetSpacesGrouped(grouped) => {
                    self.config.preferences.space_groups.grouped = grouped;
                    self.workspace_scroll = 0;
                    self.workspace_press = None;
                    if matches!(self.chrome_drag, Some(ClientChromeDrag::Workspace { .. })) {
                        self.chrome_drag = None;
                    }
                    self.persist_chrome_preferences(outcome);
                }
                ClientContextMenuAction::NewSpaceGroup => {
                    self.open_new_space_group_overlay(endpoint_id, None)
                }
                _ => {}
            },
            #[cfg(feature = "watchtower")]
            ClientContextMenuTarget::SpaceGroup {
                endpoint_id,
                group_id,
            } => {
                if !self
                    .endpoints
                    .iter()
                    .any(|endpoint| endpoint.endpoint_id == endpoint_id)
                {
                    self.space_group_error("This endpoint is no longer available.", outcome);
                    return;
                }
                let endpoint = endpoint_id.storage_key();
                if action == ClientContextMenuAction::RenameSpaceGroup {
                    let name = self
                        .config
                        .preferences
                        .space_groups
                        .groups
                        .iter()
                        .find(|group| group.endpoint == endpoint && group.id == group_id)
                        .map(|group| group.name.clone());
                    if let Some(name) = name {
                        self.overlay = Some(ClientShellOverlay::Rename(ClientRenameOverlay {
                            title: "rename group",
                            input: TextEditor::new(&name, false),
                            target: ClientRenameTarget::SpaceGroup {
                                endpoint_id,
                                group_id,
                            },
                        }));
                    } else {
                        self.space_group_error("This group no longer exists.", outcome);
                    }
                } else {
                    let result = match action {
                        ClientContextMenuAction::MoveSpaceGroup(delta) => self
                            .config
                            .preferences
                            .space_groups
                            .move_group(&endpoint, &group_id, delta),
                        ClientContextMenuAction::RemoveSpaceGroup => self
                            .config
                            .preferences
                            .space_groups
                            .remove(&endpoint, &group_id),
                        _ => return,
                    };
                    self.finish_space_group_change(result, outcome);
                }
            }
            #[cfg(feature = "watchtower")]
            ClientContextMenuTarget::WorkspaceGroups { workspace, groups } => {
                if self.space_workspace_family(&workspace).is_none() {
                    self.space_group_error(
                        "This workspace changed or closed. Reopen its menu.",
                        outcome,
                    );
                } else if action == ClientContextMenuAction::NewSpaceGroup {
                    self.open_new_space_group_overlay(
                        workspace.endpoint_id.clone(),
                        Some(workspace),
                    );
                } else if let ClientContextMenuAction::AssignSpaceGroup(index) = action {
                    let group_id = match index {
                        Some(index) => match groups.get(index) {
                            Some((id, _)) => Some(id.as_str()),
                            None => return,
                        },
                        None => None,
                    };
                    let family = self.space_workspace_family(&workspace).unwrap_or_default();
                    let result = self.config.preferences.space_groups.assign(
                        &workspace.endpoint_id.storage_key(),
                        &family,
                        group_id,
                    );
                    self.finish_space_group_change(result, outcome);
                }
            }
            ClientContextMenuTarget::AgentPanel { .. } => {
                let Some(snapshot) = self.snapshot.as_deref() else {
                    return;
                };
                match action {
                    ClientContextMenuAction::SetAgentSort(sort)
                        if snapshot.agent_view_label.is_none() =>
                    {
                        self.config.agent_panel_sort = sort;
                        self.agent_panel_sort_manual = true;
                    }
                    #[cfg(feature = "watchtower")]
                    ClientContextMenuAction::SetAgentWorkspaceFilter(enabled) => {
                        self.config.agent_current_workspace_only = enabled;
                    }
                    _ => return,
                }
                self.agent_scroll = 0;
                self.persist_chrome_preferences(outcome);
            }
            ClientContextMenuTarget::Workspace {
                workspace_id,
                #[cfg(feature = "watchtower")]
                scope,
                ..
            } => {
                #[cfg(feature = "watchtower")]
                {
                    if self.space_workspace_family(&scope).is_none() {
                        self.space_group_error(
                            "This workspace changed or closed. Reopen its menu.",
                            outcome,
                        );
                        return;
                    }
                    if action == ClientContextMenuAction::MoveToSpaceGroup {
                        let endpoint = scope.endpoint_id.storage_key();
                        let groups = self
                            .config
                            .preferences
                            .space_groups
                            .groups
                            .iter()
                            .filter(|group| group.endpoint == endpoint)
                            .map(|group| (group.id.clone(), group.name.clone()))
                            .collect();
                        self.overlay =
                            Some(ClientShellOverlay::ContextMenu(ClientContextMenuOverlay {
                                target: ClientContextMenuTarget::WorkspaceGroups {
                                    workspace: scope,
                                    groups,
                                },
                                x: menu.x,
                                y: menu.y,
                                highlighted: 0,
                            }));
                        outcome.repaint = true;
                        return;
                    }
                    if scope.endpoint_id != self.active_endpoint_id {
                        self.space_group_error(
                            "The active endpoint changed. Reopen the workspace menu.",
                            outcome,
                        );
                        return;
                    }
                }
                self.activate_workspace_context_action(workspace_id, action, outcome)
            }
            ClientContextMenuTarget::Tab {
                tab_id,
                workspace_id,
            } => self.activate_tab_context_action(tab_id, workspace_id, action, outcome),
            ClientContextMenuTarget::Pane {
                pane_id,
                workspace_id,
                source_pane_id,
                right_click_passthrough,
                ..
            } => self.activate_pane_context_action(
                pane_id,
                workspace_id,
                source_pane_id,
                right_click_passthrough,
                action,
                outcome,
            ),
        }
        outcome.repaint = true;
    }

    fn activate_workspace_context_action(
        &mut self,
        workspace_id: String,
        action: ClientContextMenuAction,
        outcome: &mut ClientShellInput,
    ) {
        use crate::input::KeybindAction;

        match action {
            ClientContextMenuAction::Rename => {
                let label = self
                    .snapshot
                    .as_deref()
                    .and_then(|snapshot| {
                        snapshot
                            .workspaces
                            .iter()
                            .find(|workspace| workspace.workspace_id == workspace_id)
                    })
                    .map(|workspace| workspace.label.clone());
                if let Some(label) = label {
                    self.overlay = Some(ClientShellOverlay::Rename(ClientRenameOverlay {
                        title: "rename workspace",
                        input: TextEditor::new(&label, false),
                        target: ClientRenameTarget::Workspace { workspace_id },
                    }));
                }
            }
            ClientContextMenuAction::Close => {
                if self.config.confirm_close {
                    self.open_confirm_close_overlay(workspace_id);
                } else {
                    self.push_endpoint_method(
                        crate::api::schema::Method::WorkspaceClose(
                            crate::api::schema::WorkspaceCloseParams {
                                workspace_id,
                                close_group: true,
                            },
                        ),
                        outcome,
                    );
                }
            }
            ClientContextMenuAction::NewWorktree => {
                self.begin_worktree_action_for(KeybindAction::NewWorktree, workspace_id, outcome)
            }
            ClientContextMenuAction::OpenWorktree => {
                self.begin_worktree_action_for(KeybindAction::OpenWorktree, workspace_id, outcome)
            }
            ClientContextMenuAction::RemoveWorktree => {
                self.begin_worktree_action_for(KeybindAction::RemoveWorktree, workspace_id, outcome)
            }
            ClientContextMenuAction::ToggleGroup => {
                let key = self.snapshot.as_deref().and_then(|snapshot| {
                    snapshot
                        .workspaces
                        .iter()
                        .find(|workspace| workspace.workspace_id == workspace_id)
                        .and_then(|workspace| workspace.worktree.as_ref())
                        .map(|worktree| worktree.key.clone())
                });
                if let Some(key) = key {
                    let endpoint_id = self.active_endpoint_id.clone();
                    self.toggle_collapsed_group(&endpoint_id, key);
                    self.persist_chrome_preferences(outcome);
                }
            }
            _ => {}
        }
    }

    fn activate_tab_context_action(
        &mut self,
        tab_id: String,
        workspace_id: String,
        action: ClientContextMenuAction,
        outcome: &mut ClientShellInput,
    ) {
        use crate::api::schema::{Method, TabTarget};

        self.push_endpoint_method(
            Method::TabFocus(TabTarget {
                tab_id: tab_id.clone(),
            }),
            outcome,
        );
        match action {
            ClientContextMenuAction::NewTab => {
                if self.config.prompt_new_tab_name {
                    let default_name = (self
                        .snapshot
                        .as_deref()
                        .map(|snapshot| {
                            snapshot
                                .tabs
                                .iter()
                                .filter(|tab| tab.workspace_id == workspace_id)
                                .count()
                        })
                        .unwrap_or(0)
                        + 1)
                    .to_string();
                    self.overlay = Some(ClientShellOverlay::Rename(ClientRenameOverlay {
                        title: "new tab",
                        input: TextEditor::new(&default_name, true),
                        target: ClientRenameTarget::NewTab {
                            workspace_id,
                            default_name,
                        },
                    }));
                } else {
                    self.push_endpoint_method(
                        Method::TabCreate(crate::api::schema::TabCreateParams {
                            workspace_id: Some(workspace_id),
                            cwd: None,
                            focus: true,
                            label: None,
                            env: Default::default(),
                        }),
                        outcome,
                    );
                }
            }
            ClientContextMenuAction::Rename => {
                let tab = self
                    .snapshot
                    .as_deref()
                    .and_then(|snapshot| snapshot.tabs.iter().find(|tab| tab.tab_id == tab_id));
                if let Some(tab) = tab {
                    self.overlay = Some(ClientShellOverlay::Rename(ClientRenameOverlay {
                        title: "rename tab",
                        input: TextEditor::new(&tab.label, false),
                        target: ClientRenameTarget::Tab {
                            tab_id,
                            auto_name: !tab.custom_label,
                            original_name: tab.label.clone(),
                        },
                    }));
                }
            }
            ClientContextMenuAction::Close => {
                self.push_endpoint_method(Method::TabClose(TabTarget { tab_id }), outcome);
            }
            _ => {}
        }
    }

    fn activate_pane_context_action(
        &mut self,
        pane_id: String,
        workspace_id: String,
        source_pane_id: Option<String>,
        right_click_passthrough: bool,
        action: ClientContextMenuAction,
        outcome: &mut ClientShellInput,
    ) {
        use crate::api::schema::{
            Method, PaneInputSetParams, PaneRenameParams, PaneRightClickTarget, PaneSplitParams,
            PaneSwapParams, PaneTarget, PaneZoomMode, PaneZoomParams, SplitDirection,
        };

        match action {
            ClientContextMenuAction::RenamePane => {
                let label = self.snapshot.as_deref().and_then(|snapshot| {
                    snapshot
                        .panes
                        .iter()
                        .find(|pane| pane.pane_id == pane_id)
                        .and_then(|pane| pane.label.clone())
                });
                self.overlay = Some(ClientShellOverlay::Rename(ClientRenameOverlay {
                    title: "rename pane",
                    input: TextEditor::new(label.as_deref().unwrap_or_default(), label.is_none()),
                    target: ClientRenameTarget::Pane { pane_id },
                }));
            }
            ClientContextMenuAction::ClearPaneName => self.push_endpoint_method(
                Method::PaneRename(PaneRenameParams {
                    pane_id,
                    label: None,
                }),
                outcome,
            ),
            ClientContextMenuAction::SwapWithFocusedPane => {
                if let Some(source_pane_id) = source_pane_id {
                    self.push_endpoint_method(
                        Method::PaneSwap(PaneSwapParams {
                            pane_id: None,
                            direction: None,
                            source_pane_id: Some(source_pane_id.clone()),
                            target_pane_id: Some(pane_id),
                        }),
                        outcome,
                    );
                    self.push_endpoint_method(
                        Method::PaneFocus(PaneTarget {
                            pane_id: source_pane_id,
                        }),
                        outcome,
                    );
                }
            }
            ClientContextMenuAction::SplitRight | ClientContextMenuAction::SplitDown => {
                self.push_endpoint_method(
                    Method::PaneSplit(PaneSplitParams {
                        workspace_id: Some(workspace_id),
                        target_pane_id: Some(pane_id),
                        direction: if action == ClientContextMenuAction::SplitRight {
                            SplitDirection::Right
                        } else {
                            SplitDirection::Down
                        },
                        ratio: None,
                        cwd: None,
                        focus: true,
                        right_click: Default::default(),
                        env: Default::default(),
                    }),
                    outcome,
                );
            }
            ClientContextMenuAction::Zoom => self.push_endpoint_method(
                Method::PaneZoom(PaneZoomParams {
                    pane_id: Some(pane_id),
                    mode: PaneZoomMode::Toggle,
                }),
                outcome,
            ),
            ClientContextMenuAction::ToggleRightClickPassthrough => self.push_endpoint_method(
                Method::PaneInputSet(PaneInputSetParams {
                    pane_id,
                    right_click: if right_click_passthrough {
                        PaneRightClickTarget::Herdr
                    } else {
                        PaneRightClickTarget::Pane
                    },
                }),
                outcome,
            ),
            ClientContextMenuAction::ClosePane => {
                self.push_endpoint_method(Method::PaneClose(PaneTarget { pane_id }), outcome)
            }
            _ => {}
        }
    }
}
