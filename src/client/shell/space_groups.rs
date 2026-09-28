//! Client-only workspace collections. These never alter endpoint workspace identities.
use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};

use crate::{
    api::schema::AgentStatus, client::endpoint::ClientEndpointId, protocol::ClientShellSnapshot,
};

use super::WorkspaceEntry;

#[cfg(any(feature = "watchtower", test))]
use crate::client::endpoint::ProfileId;

#[cfg(any(feature = "watchtower", test))]
const MAX_NAME_CHARS: usize = 64;

#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
pub(super) struct SpaceGroups {
    #[serde(default)]
    pub(super) grouped: bool,
    #[serde(default)]
    pub(super) groups: Vec<SpaceGroup>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub(super) struct SpaceGroup {
    pub(super) id: String,
    pub(super) name: String,
    pub(super) endpoint: String,
    #[serde(default)]
    pub(super) workspace_ids: Vec<String>,
    #[serde(default)]
    pub(super) collapsed: bool,
}

pub(super) fn endpoint_key(endpoint: &ClientEndpointId) -> String {
    endpoint.storage_key()
}

#[cfg(any(feature = "watchtower", test))]
impl SpaceGroups {
    fn validated_name(
        &self,
        endpoint: &str,
        id: Option<&str>,
        name: &str,
    ) -> Result<String, String> {
        if name.chars().any(char::is_control) {
            return Err("Group names cannot contain control characters.".into());
        }
        let name = name.trim();
        if name.is_empty() || name.chars().count() > MAX_NAME_CHARS {
            return Err(format!(
                "Use a group name between 1 and {MAX_NAME_CHARS} characters."
            ));
        }
        if self.groups.iter().any(|group| {
            group.endpoint == endpoint
                && Some(group.id.as_str()) != id
                && group.name.to_lowercase() == name.to_lowercase()
        }) {
            return Err("A group with that name already exists on this connection.".into());
        }
        Ok(name.to_owned())
    }

    fn index(&self, endpoint: &str, id: &str) -> Result<usize, String> {
        self.groups
            .iter()
            .position(|group| group.endpoint == endpoint && group.id == id)
            .ok_or_else(|| "This group is no longer available on this connection.".into())
    }

    pub(super) fn create(&mut self, endpoint: &str, name: &str) -> Result<String, String> {
        let name = self.validated_name(endpoint, None, name)?;
        let id = format!("space-{}", ProfileId::generate());
        self.groups.push(SpaceGroup {
            id: id.clone(),
            name,
            endpoint: endpoint.to_owned(),
            workspace_ids: Vec::new(),
            collapsed: false,
        });
        Ok(id)
    }

    pub(super) fn rename(&mut self, endpoint: &str, id: &str, name: &str) -> Result<(), String> {
        let index = self.index(endpoint, id)?;
        let name = self.validated_name(endpoint, Some(id), name)?;
        self.groups[index].name = name;
        Ok(())
    }

    /// The caller passes the selected workspace's entire current worktree family.
    /// Unknown saved IDs are retained: an offline/partial snapshot is not deletion evidence.
    pub(super) fn assign(
        &mut self,
        endpoint: &str,
        workspace_ids: &[String],
        group_id: Option<&str>,
    ) -> Result<(), String> {
        let target = group_id.map(|id| self.index(endpoint, id)).transpose()?;
        if workspace_ids
            .iter()
            .any(|id| id.is_empty() || id.len() > 256 || id.chars().any(char::is_control))
        {
            return Err("Choose valid workspace IDs for this connection.".into());
        }
        let selected = workspace_ids
            .iter()
            .map(String::as_str)
            .collect::<HashSet<_>>();
        for group in self
            .groups
            .iter_mut()
            .filter(|group| group.endpoint == endpoint)
        {
            group
                .workspace_ids
                .retain(|id| !selected.contains(id.as_str()));
        }
        if let Some(index) = target {
            let mut added = HashSet::new();
            self.groups[index].workspace_ids.extend(
                workspace_ids
                    .iter()
                    .filter(|id| added.insert(id.as_str()))
                    .cloned(),
            );
        }
        Ok(())
    }

    pub(super) fn toggle(&mut self, endpoint: &str, id: &str) -> Result<(), String> {
        let index = self.index(endpoint, id)?;
        self.groups[index].collapsed = !self.groups[index].collapsed;
        Ok(())
    }

    pub(super) fn move_group(
        &mut self,
        endpoint: &str,
        id: &str,
        delta: isize,
    ) -> Result<(), String> {
        let index = self.index(endpoint, id)?;
        let indices = self
            .groups
            .iter()
            .enumerate()
            .filter_map(|(index, group)| (group.endpoint == endpoint).then_some(index))
            .collect::<Vec<_>>();
        let Some(position) = indices.iter().position(|candidate| *candidate == index) else {
            return Ok(());
        };
        let target = position
            .saturating_add_signed(delta)
            .min(indices.len().saturating_sub(1));
        // Shift only this endpoint's slots; other connections keep their order and positions.
        if target > position {
            for current in position..target {
                self.groups.swap(indices[current], indices[current + 1]);
            }
        } else {
            for current in (target..position).rev() {
                self.groups.swap(indices[current], indices[current + 1]);
            }
        }
        Ok(())
    }

    pub(super) fn remove(&mut self, endpoint: &str, id: &str) -> Result<(), String> {
        let index = self.index(endpoint, id)?;
        self.groups.remove(index);
        Ok(())
    }
}

#[derive(Clone, Copy)]
pub(super) enum SpaceRow {
    Group {
        index: usize,
        count: usize,
        status: AgentStatus,
    },
    Ungrouped {
        count: usize,
        status: AgentStatus,
    },
    Workspace(WorkspaceEntry),
}

/// Return current family membership without changing saved preferences.
#[cfg(any(feature = "watchtower", test))]
pub(super) fn family_workspace_ids(snapshot: &ClientShellSnapshot, id: &str) -> Vec<String> {
    let Some(workspace) = snapshot
        .workspaces
        .iter()
        .find(|workspace| workspace.workspace_id == id)
    else {
        return Vec::new();
    };
    let Some(worktree) = workspace.worktree.as_ref() else {
        return vec![workspace.workspace_id.clone()];
    };
    snapshot
        .workspaces
        .iter()
        .filter(|candidate| {
            candidate
                .worktree
                .as_ref()
                .is_some_and(|candidate| candidate.key == worktree.key)
        })
        .map(|candidate| candidate.workspace_id.clone())
        .collect()
}

pub(super) fn rows(
    snapshot: &ClientShellSnapshot,
    worktree_collapsed: &HashSet<String>,
    endpoint: &ClientEndpointId,
    groups: &SpaceGroups,
) -> Vec<SpaceRow> {
    let entries = super::render::workspace_entries(snapshot, worktree_collapsed);
    if !cfg!(feature = "watchtower") || !groups.grouped {
        return entries.into_iter().map(SpaceRow::Workspace).collect();
    }
    let endpoint = endpoint_key(endpoint);
    let group_indices = groups
        .groups
        .iter()
        .enumerate()
        .filter_map(|(index, group)| (group.endpoint == endpoint).then_some(index))
        .collect::<Vec<_>>();
    // Only borrowed IDs are indexed; no family scan is repeated for each rendered row.
    let mut assigned = HashMap::<&str, usize>::new();
    for (slot, &index) in group_indices.iter().enumerate() {
        for id in &groups.groups[index].workspace_ids {
            assigned.entry(id).or_insert(slot);
        }
    }
    // (parent's explicit group, earliest explicit group) for each worktree family.
    let mut families = HashMap::<&str, (Option<usize>, Option<usize>)>::new();
    for workspace in &snapshot.workspaces {
        let Some(worktree) = workspace.worktree.as_ref() else {
            continue;
        };
        let Some(&slot) = assigned.get(workspace.workspace_id.as_str()) else {
            continue;
        };
        let family = families.entry(&worktree.key).or_default();
        if !worktree.is_linked_worktree {
            family.0 = Some(family.0.map_or(slot, |previous| previous.min(slot)));
        }
        family.1 = Some(family.1.map_or(slot, |previous| previous.min(slot)));
    }
    let ungrouped = group_indices.len();
    let mut destinations = Vec::with_capacity(snapshot.workspaces.len());
    let mut counts = vec![0; group_indices.len() + 1];
    let mut statuses = vec![AgentStatus::Unknown; group_indices.len() + 1];
    for workspace in &snapshot.workspaces {
        let slot = workspace
            .worktree
            .as_ref()
            .and_then(|worktree| families.get(worktree.key.as_str()))
            .and_then(|(parent, first)| parent.or(*first))
            .or_else(|| assigned.get(workspace.workspace_id.as_str()).copied())
            .unwrap_or(ungrouped);
        destinations.push(slot);
        counts[slot] += 1;
        if super::status_priority(workspace.agent_status) > super::status_priority(statuses[slot]) {
            statuses[slot] = workspace.agent_status;
        }
    }
    let mut buckets = vec![Vec::new(); group_indices.len() + 1];
    for entry in entries {
        let slot = destinations[entry.index];
        let collapsed = slot != ungrouped && groups.groups[group_indices[slot]].collapsed;
        if !collapsed {
            buckets[slot].push(entry);
        } else if snapshot.workspaces[entry.index].focused {
            // The group header replaces the tree context when collapsed. Keep the active
            // workspace reachable without drawing an orphaned child connector.
            buckets[slot].push(WorkspaceEntry {
                indented: false,
                last_child: false,
                ..entry
            });
        }
    }
    let mut result = Vec::with_capacity(snapshot.workspaces.len() + group_indices.len() + 1);
    for (slot, &index) in group_indices.iter().enumerate() {
        result.push(SpaceRow::Group {
            index,
            count: counts[slot],
            status: statuses[slot],
        });
        result.extend(buckets[slot].drain(..).map(SpaceRow::Workspace));
    }
    if counts[ungrouped] > 0 {
        result.push(SpaceRow::Ungrouped {
            count: counts[ungrouped],
            status: statuses[ungrouped],
        });
        result.extend(buckets[ungrouped].drain(..).map(SpaceRow::Workspace));
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::ClientShellWorktree;

    fn family_snapshot() -> ClientShellSnapshot {
        let mut snapshot = crate::client::shell::tests::snapshot();
        let template = snapshot.workspaces[0].clone();
        snapshot.workspaces = (0..4)
            .map(|index| {
                let mut workspace = template.clone();
                workspace.workspace_id = format!("w{index}");
                workspace.focused = index == 2;
                workspace.agent_status = if index == 1 {
                    AgentStatus::Blocked
                } else {
                    AgentStatus::Idle
                };
                workspace.worktree = (index < 3).then(|| ClientShellWorktree {
                    key: "repo".into(),
                    label: "Repo".into(),
                    is_linked_worktree: index != 0,
                });
                workspace
            })
            .collect();
        snapshot.focused_workspace_id = Some("w2".into());
        snapshot
    }

    fn workspace_indices(rows: &[SpaceRow]) -> Vec<(usize, bool, bool)> {
        rows.iter()
            .filter_map(|row| match row {
                SpaceRow::Workspace(entry) => Some((entry.index, entry.indented, entry.last_child)),
                SpaceRow::Group { .. } | SpaceRow::Ungrouped { .. } => None,
            })
            .collect()
    }

    #[test]
    fn names_are_bounded_unique_per_endpoint_and_ids_survive_rename() {
        let mut groups = SpaceGroups::default();
        for name in ["", "  ", "ok\n", "esc\u{1b}", &"x".repeat(65)] {
            assert!(groups.create("local", name).is_err());
        }
        let id = groups.create("local", "  Project A  ").unwrap();
        assert_eq!(groups.groups[0].name, "Project A");
        assert!(groups.create("local", "project a").is_err());
        assert!(groups.create("ssh:other", "Project A").is_ok());
        groups.rename("local", &id, "Project B").unwrap();
        assert_eq!(groups.groups[0].id, id);
        assert!(groups.rename("ssh:other", &id, "Invalid").is_err());
        assert!(groups.toggle("ssh:other", &id).is_err());
        assert!(groups.remove("ssh:other", &id).is_err());
    }

    #[test]
    fn assignment_is_unique_atomic_and_endpoint_scoped() {
        let mut groups = SpaceGroups::default();
        let first = groups.create("local", "First").unwrap();
        let remote = groups.create("ssh:other", "Remote").unwrap();
        let second = groups.create("local", "Second").unwrap();
        let ids = vec!["w0".to_owned(), "w0".to_owned(), "w1".to_owned()];
        groups.assign("local", &ids, Some(&first)).unwrap();
        groups.assign("ssh:other", &ids, Some(&remote)).unwrap();
        assert_eq!(groups.groups[0].workspace_ids, ["w0", "w1"]);
        let saved = groups.clone();
        assert!(groups.assign("local", &ids, Some(&remote)).is_err());
        assert!(groups.assign("local", &["bad\n".into()], None).is_err());
        assert_eq!(groups, saved);
        groups.assign("local", &ids, Some(&second)).unwrap();
        assert!(groups.groups[0].workspace_ids.is_empty());
        assert_eq!(groups.groups[1].workspace_ids, ["w0", "w1"]);
        groups.assign("local", &ids, None).unwrap();
        assert!(groups.groups[2].workspace_ids.is_empty());
    }

    #[test]
    fn group_order_changes_only_selected_endpoints_slots() {
        let mut groups = SpaceGroups::default();
        let first = groups.create("local", "First").unwrap();
        let remote = groups.create("ssh:other", "Remote").unwrap();
        let second = groups.create("local", "Second").unwrap();
        let third = groups.create("local", "Third").unwrap();
        groups.move_group("local", &third, -20).unwrap();
        assert_eq!(
            groups
                .groups
                .iter()
                .map(|group| group.id.as_str())
                .collect::<Vec<_>>(),
            [
                third.as_str(),
                remote.as_str(),
                first.as_str(),
                second.as_str()
            ]
        );
        groups.move_group("local", &third, 20).unwrap();
        assert_eq!(groups.groups[0].id, first);
        assert_eq!(groups.groups[1].id, remote);
        groups.remove("local", &second).unwrap();
        assert_eq!(groups.groups.len(), 3);
    }

    #[test]
    fn flat_projection_is_identical_to_worktree_projection() {
        let snapshot = family_snapshot();
        for collapsed in [HashSet::new(), HashSet::from(["repo".into()])] {
            let actual = rows(
                &snapshot,
                &collapsed,
                &ClientEndpointId::Local,
                &SpaceGroups::default(),
            );
            let expected = super::super::render::workspace_entries(&snapshot, &collapsed)
                .iter()
                .map(|entry| (entry.index, entry.indented, entry.last_child))
                .collect::<Vec<_>>();
            assert_eq!(workspace_indices(&actual), expected);
        }
    }

    #[test]
    fn family_helper_includes_parent_children_and_handles_missing_workspace() {
        let snapshot = family_snapshot();
        assert_eq!(family_workspace_ids(&snapshot, "w2"), ["w0", "w1", "w2"]);
        assert_eq!(family_workspace_ids(&snapshot, "w3"), ["w3"]);
        assert!(family_workspace_ids(&snapshot, "missing").is_empty());
    }

    #[test]
    #[cfg(feature = "watchtower")]
    fn groups_include_empty_rows_whole_families_and_ungrouped_tail() {
        let snapshot = family_snapshot();
        let mut groups = SpaceGroups {
            grouped: true,
            ..SpaceGroups::default()
        };
        let empty = groups.create("local", "Empty").unwrap();
        let assigned = groups.create("local", "Family").unwrap();
        groups.create("ssh:other", "Remote").unwrap();
        groups
            .assign("local", &["w0".into(), "deleted".into()], Some(&assigned))
            .unwrap();
        let saved = groups.clone();
        let actual = rows(
            &snapshot,
            &HashSet::new(),
            &ClientEndpointId::Local,
            &groups,
        );
        assert!(matches!(
            actual[0],
            SpaceRow::Group {
                index: 0,
                count: 0,
                status: AgentStatus::Unknown
            }
        ));
        assert!(matches!(
            actual[1],
            SpaceRow::Group {
                index: 1,
                count: 3,
                status: AgentStatus::Blocked
            }
        ));
        assert_eq!(
            workspace_indices(&actual),
            [
                (0, false, false),
                (1, true, false),
                (2, true, true),
                (3, false, false)
            ]
        );
        assert_eq!(groups, saved, "projection must not prune stale assignments");
        assert_eq!(groups.groups[0].id, empty);
        assert!(matches!(
            actual[actual.len() - 2],
            SpaceRow::Ungrouped {
                count: 1,
                status: AgentStatus::Idle
            }
        ));
    }

    #[test]
    #[cfg(feature = "watchtower")]
    fn ungrouped_header_counts_hidden_worktrees_and_is_absent_when_everyone_is_assigned() {
        let snapshot = family_snapshot();
        let mut groups = SpaceGroups {
            grouped: true,
            ..SpaceGroups::default()
        };
        let id = groups.create("local", "Assigned").unwrap();
        groups.assign("local", &["w3".into()], Some(&id)).unwrap();
        let actual = rows(
            &snapshot,
            &HashSet::from(["repo".into()]),
            &ClientEndpointId::Local,
            &groups,
        );
        assert!(matches!(
            actual[2],
            SpaceRow::Ungrouped {
                count: 3,
                status: AgentStatus::Blocked
            }
        ));
        assert_eq!(
            workspace_indices(&actual),
            [(3, false, false), (0, false, false), (2, true, true)]
        );
        groups.assign("local", &["w0".into()], Some(&id)).unwrap();
        let actual = rows(
            &snapshot,
            &HashSet::new(),
            &ClientEndpointId::Local,
            &groups,
        );
        assert!(!actual
            .iter()
            .any(|row| matches!(row, SpaceRow::Ungrouped { .. })));
        let empty = rows(
            &ClientShellSnapshot {
                workspaces: Vec::new(),
                ..snapshot
            },
            &HashSet::new(),
            &ClientEndpointId::Local,
            &groups,
        );
        assert!(matches!(&empty[..], [SpaceRow::Group { count: 0, .. }]));
    }

    #[test]
    #[cfg(feature = "watchtower")]
    fn collapsed_groups_retain_focus_and_all_member_attention_counts() {
        let snapshot = family_snapshot();
        let mut groups = SpaceGroups {
            grouped: true,
            ..SpaceGroups::default()
        };
        let id = groups.create("local", "Family").unwrap();
        groups.assign("local", &["w0".into()], Some(&id)).unwrap();
        groups.toggle("local", &id).unwrap();
        for collapsed in [HashSet::new(), HashSet::from(["repo".into()])] {
            let actual = rows(&snapshot, &collapsed, &ClientEndpointId::Local, &groups);
            assert!(matches!(
                actual[0],
                SpaceRow::Group {
                    count: 3,
                    status: AgentStatus::Blocked,
                    ..
                }
            ));
            assert_eq!(
                workspace_indices(&actual),
                [(2, false, false), (3, false, false)]
            );
        }
    }

    #[test]
    #[cfg(feature = "watchtower")]
    fn parent_membership_resolves_conflicts_and_child_membership_groups_orphans() {
        let mut snapshot = family_snapshot();
        let mut groups = SpaceGroups {
            grouped: true,
            ..SpaceGroups::default()
        };
        let first = groups.create("local", "First").unwrap();
        let parent = groups.create("local", "Parent").unwrap();
        groups
            .assign("local", &["w1".into()], Some(&first))
            .unwrap();
        groups
            .assign("local", &["w0".into()], Some(&parent))
            .unwrap();
        let actual = rows(
            &snapshot,
            &HashSet::new(),
            &ClientEndpointId::Local,
            &groups,
        );
        assert!(matches!(actual[0], SpaceRow::Group { count: 0, .. }));
        assert!(matches!(actual[1], SpaceRow::Group { count: 3, .. }));
        snapshot.workspaces.remove(0);
        let actual = rows(
            &snapshot,
            &HashSet::new(),
            &ClientEndpointId::Local,
            &groups,
        );
        assert!(matches!(actual[0], SpaceRow::Group { count: 2, .. }));
        assert_eq!(
            workspace_indices(&actual),
            [(0, false, false), (1, false, false), (2, false, false)]
        );
    }

    #[test]
    #[cfg(feature = "watchtower")]
    fn same_workspace_id_on_remote_endpoint_has_independent_membership() {
        let snapshot = family_snapshot();
        let remote =
            ClientEndpointId::Ssh(ProfileId::parse("0123456789abcdef0123456789abcdef").unwrap());
        let mut groups = SpaceGroups {
            grouped: true,
            ..SpaceGroups::default()
        };
        let local_id = groups.create("local", "Local").unwrap();
        let remote_id = groups.create(&endpoint_key(&remote), "Remote").unwrap();
        groups
            .assign("local", &["w3".into()], Some(&local_id))
            .unwrap();
        groups
            .assign(&endpoint_key(&remote), &["w0".into()], Some(&remote_id))
            .unwrap();
        let actual = rows(&snapshot, &HashSet::new(), &remote, &groups);
        assert!(matches!(
            actual[0],
            SpaceRow::Group {
                index: 1,
                count: 3,
                ..
            }
        ));
        assert_eq!(endpoint_key(&ClientEndpointId::Local), "local");
        assert_eq!(
            endpoint_key(&remote),
            "ssh:0123456789abcdef0123456789abcdef"
        );
    }
}
