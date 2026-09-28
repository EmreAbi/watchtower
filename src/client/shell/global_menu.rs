use super::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum ClientGlobalMenuAction {
    Binding(crate::input::KeybindAction),
    WhatsNew,
    #[cfg(feature = "watchtower")]
    Accounts,
    #[cfg(feature = "watchtower")]
    Results,
    #[cfg(feature = "watchtower")]
    Teams,
    #[cfg(feature = "watchtower")]
    ModelLab,
}

pub(super) fn global_menu_attention(snapshot: &ClientShellSnapshot) -> bool {
    snapshot.update_available.is_some() || snapshot.integration_updates_available
}

pub(super) fn global_menu_item_has_badge(
    snapshot: &ClientShellSnapshot,
    action: ClientGlobalMenuAction,
) -> bool {
    (action == ClientGlobalMenuAction::WhatsNew && snapshot.update_available.is_some())
        || (action == ClientGlobalMenuAction::Binding(crate::input::KeybindAction::Settings)
            && snapshot.integration_updates_available)
}

pub(super) fn global_menu_items(
    snapshot: &ClientShellSnapshot,
) -> Vec<(&'static str, ClientGlobalMenuAction)> {
    let mut items = vec![
        (
            "settings",
            ClientGlobalMenuAction::Binding(crate::input::KeybindAction::Settings),
        ),
        (
            "keybinds",
            ClientGlobalMenuAction::Binding(crate::input::KeybindAction::Help),
        ),
        (
            "reload config",
            ClientGlobalMenuAction::Binding(crate::input::KeybindAction::ReloadConfig),
        ),
    ];
    if snapshot.update_available.is_some() || snapshot.latest_release_notes_available {
        items.push((
            if snapshot.update_available.is_some() {
                "update ready"
            } else {
                "what's new"
            },
            ClientGlobalMenuAction::WhatsNew,
        ));
    }
    #[cfg(feature = "watchtower")]
    items.push(("accounts", ClientGlobalMenuAction::Accounts));
    #[cfg(feature = "watchtower")]
    items.push(("results", ClientGlobalMenuAction::Results));
    #[cfg(feature = "watchtower")]
    items.push(("teams", ClientGlobalMenuAction::Teams));
    #[cfg(feature = "watchtower")]
    items.push(("model lab", ClientGlobalMenuAction::ModelLab));
    items.push((
        "detach",
        ClientGlobalMenuAction::Binding(crate::input::KeybindAction::Detach),
    ));
    items
}

impl ClientShellState {
    pub(super) fn toggle_global_menu(&mut self) {
        if matches!(self.overlay, Some(ClientShellOverlay::GlobalMenu(_))) {
            self.overlay = None;
        } else {
            self.overlay = Some(ClientShellOverlay::GlobalMenu(ClientGlobalMenuOverlay {
                highlighted: 0,
            }));
        }
    }

    pub(super) fn move_global_menu_selection(&mut self, delta: isize) {
        let item_count = self
            .snapshot
            .as_deref()
            .map(global_menu_items)
            .map_or(0, |items| items.len());
        let Some(ClientShellOverlay::GlobalMenu(menu)) = self.overlay.as_mut() else {
            return;
        };
        menu.highlighted = (menu.highlighted as isize + delta)
            .clamp(0, item_count.saturating_sub(1) as isize) as usize;
    }

    pub(super) fn activate_global_menu_item(
        &mut self,
        index: usize,
        outcome: &mut ClientShellInput,
    ) {
        let Some(action) = self.snapshot.as_deref().and_then(|snapshot| {
            global_menu_items(snapshot)
                .get(index)
                .map(|(_, action)| *action)
        }) else {
            return;
        };
        if action == ClientGlobalMenuAction::WhatsNew
            && self
                .snapshot
                .as_deref()
                .and_then(|snapshot| snapshot.release_notes.as_ref())
                .is_none()
        {
            return;
        }
        self.overlay = None;
        match action {
            ClientGlobalMenuAction::Binding(binding) => {
                self.record_binding(crate::input::KeybindMatch::Action(binding), outcome)
            }
            ClientGlobalMenuAction::WhatsNew => self.open_release_notes(),
            #[cfg(feature = "watchtower")]
            ClientGlobalMenuAction::Accounts => self.open_accounts_center(outcome),
            #[cfg(feature = "watchtower")]
            ClientGlobalMenuAction::Results => self.open_results_center(outcome),
            #[cfg(feature = "watchtower")]
            ClientGlobalMenuAction::Teams => self.open_teams_center(outcome),
            #[cfg(feature = "watchtower")]
            ClientGlobalMenuAction::ModelLab => self.open_model_lab(outcome),
        }
        outcome.repaint = true;
    }

    #[cfg(feature = "watchtower")]
    fn open_model_lab(&mut self, outcome: &mut ClientShellInput) {
        if self.popup_pending || self.popup_terminal_id.is_some() {
            return;
        }
        let params = crate::api::schema::PluginPaneOpenParams {
            plugin_id: "watchtower-benchmarks".into(),
            entrypoint: "center".into(),
            placement: Some(crate::api::schema::PluginPanePlacement::Popup),
            width: Some(crate::popup_size::PopupSize::Percent(94)),
            height: Some(crate::popup_size::PopupSize::Percent(94)),
            workspace_id: None,
            target_pane_id: None,
            direction: None,
            cwd: None,
            focus: true,
            env: Default::default(),
        };
        self.popup_pending = true;
        self.popup_pending_deadline = None;
        if !self.push_endpoint_method_with_kind(
            crate::api::schema::Method::PluginPaneOpen(params),
            PendingEndpointKind::PopupCommand,
            outcome,
        ) {
            self.popup_pending = false;
        }
    }

    #[cfg(feature = "watchtower")]
    fn open_teams_center(&mut self, outcome: &mut ClientShellInput) {
        if self.popup_pending || self.popup_terminal_id.is_some() {
            return;
        }
        // Presentation only; the plugin uses existing public workspace and
        // account APIs after the user reviews and confirms the setup plan.
        let params = crate::api::schema::PluginPaneOpenParams {
            plugin_id: "watchtower-teams".into(),
            entrypoint: "center".into(),
            placement: Some(crate::api::schema::PluginPanePlacement::Popup),
            width: Some(crate::popup_size::PopupSize::Percent(94)),
            height: Some(crate::popup_size::PopupSize::Percent(94)),
            workspace_id: None,
            target_pane_id: None,
            direction: None,
            cwd: None,
            focus: true,
            env: Default::default(),
        };
        self.popup_pending = true;
        self.popup_pending_deadline = None;
        if !self.push_endpoint_method_with_kind(
            crate::api::schema::Method::PluginPaneOpen(params),
            PendingEndpointKind::PopupCommand,
            outcome,
        ) {
            self.popup_pending = false;
        }
    }

    #[cfg(feature = "watchtower")]
    fn open_results_center(&mut self, outcome: &mut ClientShellInput) {
        let Some(pane_id) = self.focused_pane_id() else {
            return;
        };
        self.open_results_for_pane(pane_id, outcome);
    }

    #[cfg(feature = "watchtower")]
    pub(super) fn open_results_for_pane(
        &mut self,
        pane_id: String,
        outcome: &mut ClientShellInput,
    ) {
        if self.popup_pending
            || self.popup_terminal_id.is_some()
            || !self
                .snapshot
                .as_deref()
                .is_some_and(|snapshot| snapshot.panes.iter().any(|pane| pane.pane_id == pane_id))
        {
            return;
        }
        let params = crate::api::schema::PluginPaneOpenParams {
            plugin_id: "watchtower-results".into(),
            entrypoint: "center".into(),
            placement: Some(crate::api::schema::PluginPanePlacement::Popup),
            width: Some(crate::popup_size::PopupSize::Percent(94)),
            height: Some(crate::popup_size::PopupSize::Percent(94)),
            workspace_id: None,
            // Popup placement forbids target_pane_id, even for the focused pane.
            // Keep the Results session binding in plugin env instead.
            target_pane_id: None,
            direction: None,
            cwd: None,
            focus: true,
            env: [("WATCHTOWER_RESULTS_PANE".into(), pane_id)].into(),
        };
        self.popup_pending = true;
        self.popup_pending_deadline = None;
        if !self.push_endpoint_method_with_kind(
            crate::api::schema::Method::PluginPaneOpen(params),
            PendingEndpointKind::PopupCommand,
            outcome,
        ) {
            self.popup_pending = false;
        }
    }

    #[cfg(feature = "watchtower")]
    fn open_accounts_center(&mut self, outcome: &mut ClientShellInput) {
        // This is an existing public API action. Provider/account work runs in
        // the plugin process, never in the client render or input loop.
        let params = crate::api::schema::PluginPaneOpenParams {
            plugin_id: "watchtower-accounts".into(),
            entrypoint: "center".into(),
            placement: Some(crate::api::schema::PluginPanePlacement::Popup),
            width: Some(crate::popup_size::PopupSize::Percent(90)),
            height: Some(crate::popup_size::PopupSize::Percent(90)),
            workspace_id: None,
            target_pane_id: None,
            direction: None,
            cwd: None,
            focus: true,
            env: Default::default(),
        };
        self.popup_pending = true;
        self.popup_pending_deadline = None;
        if !self.push_endpoint_method_with_kind(
            crate::api::schema::Method::PluginPaneOpen(params),
            PendingEndpointKind::PopupCommand,
            outcome,
        ) {
            self.popup_pending = false;
        }
    }
}
