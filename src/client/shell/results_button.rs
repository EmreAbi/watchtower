use super::*;
use crate::protocol::CellData;

const BUTTON: &str = "[Results]";
const RESERVED_WIDTH: u16 = 11;
const MIN_PANE_WIDTH: u16 = 20;

/// Client chrome only: change the existing border, never the terminal viewport.
/// Inner-only surface patches therefore do not need to redraw these buttons.
pub(super) fn render_results_buttons(
    frame: &mut FrameData,
    snapshot: &ClientShellSnapshot,
    panes: &[PaneHit],
    palette: &Palette,
    hits: &mut Vec<(Rect, String)>,
) {
    hits.clear();
    for pane in panes {
        if pane.popup
            || pane.rect.width < MIN_PANE_WIDTH
            || pane.inner_rect.y <= pane.rect.y
            || pane.rect.right() > frame.width
            || pane.rect.y >= frame.height
            || !snapshot
                .agents
                .iter()
                .any(|agent| agent.pane_id == pane.pane_id)
        {
            continue;
        }
        let end = pane.rect.right().saturating_sub(1);
        let start = end.saturating_sub(RESERVED_WIDTH);
        let row_offset = usize::from(pane.rect.y) * usize::from(frame.width);
        let Some(row) = frame
            .cells
            .get_mut(row_offset..row_offset + usize::from(frame.width))
        else {
            continue;
        };
        let title_was_clipped = row[usize::from(start)..usize::from(end)]
            .iter()
            .any(|cell| !matches!(cell.symbol.as_str(), "" | " " | "─" | "━" | "═"));
        // A title can end with a two-column grapheme whose continuation falls
        // inside our reserved area. Remove the whole grapheme, not half of it.
        let crosses_boundary = row[usize::from(start - 1)].symbol.width() > 1;
        let style = Style::default()
            .fg(palette.accent)
            .add_modifier(Modifier::BOLD);
        let mut cell = ratatui::buffer::Cell::default();
        cell.set_style(style);
        if crosses_boundary {
            cell.set_symbol("…");
            row[usize::from(start - 1)] = CellData::from_ratatui_cell(&cell);
        }
        cell.set_symbol(if title_was_clipped && !crosses_boundary {
            "…"
        } else {
            " "
        });
        row[usize::from(start)] = CellData::from_ratatui_cell(&cell);
        for (offset, symbol) in BUTTON.bytes().enumerate() {
            let text = [symbol];
            cell.set_symbol(std::str::from_utf8(&text).expect("ASCII button"));
            row[usize::from(start + 1) + offset] = CellData::from_ratatui_cell(&cell);
        }
        cell.set_symbol(" ");
        row[usize::from(end - 1)] = CellData::from_ratatui_cell(&cell);
        hits.push((
            Rect::new(start + 1, pane.rect.y, BUTTON.len() as u16, 1),
            pane.pane_id.clone(),
        ));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::schema::AgentStatus;
    use crate::protocol::ClientShellAgent;

    fn fixture(count: usize) -> (FrameData, ClientShellSnapshot, Vec<PaneHit>) {
        // Fixed 240x80 geometry, including the 15-pane profiling case.
        let mut buffer = Buffer::empty(Rect::new(0, 0, 240, 80));
        let mut snapshot = crate::client::shell::tests::snapshot();
        let mut panes = Vec::new();
        for i in 0..count {
            let rect = Rect::new((i % 3) as u16 * 80, (i / 3) as u16 * 15, 80, 15);
            buffer.set_string(rect.x, rect.y, "─".repeat(80), Style::default());
            buffer.set_string(rect.x + 1, rect.y, " lead ", Style::default());
            let pane_id = format!("pane_{i}");
            snapshot.agents.push(ClientShellAgent {
                pane_id: pane_id.clone(),
                workspace_id: "ws_1".into(),
                tab_id: "tab_1".into(),
                name: Some("lead".into()),
                display_agent: None,
                agent: Some("codex".into()),
                title: None,
                terminal_title: None,
                terminal_title_stripped: None,
                agent_status: AgentStatus::Idle,
                state_change_seq: 0,
                state_labels: Vec::new(),
                tokens: Vec::new(),
                focused: i == 0,
            });
            panes.push(PaneHit {
                rect,
                inner_rect: Rect::new(rect.x + 1, rect.y + 1, 78, 13),
                scrollbar_rect: None,
                scroll: None,
                pane_id,
                popup: false,
                mouse_reporting: true,
                sgr_pixel_mouse: false,
                pixel_width: 0,
                pixel_height: 0,
            });
        }
        (
            FrameData::from_ratatui_buffer(&buffer, None),
            snapshot,
            panes,
        )
    }

    #[test]
    fn results_buttons_only_change_existing_top_border_and_keep_source_identity() {
        let (mut frame, snapshot, panes) = fixture(2);
        let original = frame.clone();
        let mut hits = Vec::new();
        render_results_buttons(
            &mut frame,
            &snapshot,
            &panes,
            &Palette::catppuccin(),
            &mut hits,
        );
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0], (Rect::new(69, 0, 9, 1), "pane_0".into()));
        assert_eq!(hits[1], (Rect::new(149, 0, 9, 1), "pane_1".into()));
        for (index, (before, after)) in original.cells.iter().zip(&frame.cells).enumerate() {
            if before != after {
                assert_eq!(index / 240, 0);
                assert!((68..79).contains(&(index % 240)) || (148..159).contains(&(index % 240)));
            }
        }
        assert_eq!(frame.cursor, original.cursor);
        assert_eq!(frame.hyperlinks, original.hyperlinks);
        assert_eq!(frame.graphics, original.graphics);
    }

    #[test]
    fn results_buttons_skip_borderless_narrow_popup_and_nonagent_panes() {
        let (mut frame, mut snapshot, mut panes) = fixture(4);
        panes[0].inner_rect.y = panes[0].rect.y;
        panes[1].rect.width = MIN_PANE_WIDTH - 1;
        panes[2].popup = true;
        snapshot.agents.pop();
        let original = frame.clone();
        let mut hits = vec![(Rect::new(1, 1, 1, 1), "stale".into())];
        render_results_buttons(
            &mut frame,
            &snapshot,
            &panes,
            &Palette::catppuccin(),
            &mut hits,
        );
        assert!(hits.is_empty());
        assert_eq!(frame, original);
    }

    #[test]
    fn results_buttons_truncate_long_title_without_orphaning_wide_grapheme() {
        let (_, snapshot, panes) = fixture(1);
        let mut buffer = Buffer::empty(Rect::new(0, 0, 240, 80));
        buffer.set_string(1, 0, "长".repeat(39), Style::default());
        let mut frame = FrameData::from_ratatui_buffer(&buffer, None);
        let mut hits = Vec::new();
        render_results_buttons(
            &mut frame,
            &snapshot,
            &panes,
            &Palette::catppuccin(),
            &mut hits,
        );
        assert_eq!(frame.cells[65].symbol, "长");
        assert_eq!(frame.cells[67].symbol, "…");
        assert_eq!(frame.cells[68].symbol, " ");
        assert_eq!(frame.cells[69].symbol, "[");
        assert_eq!(frame.cells[77].symbol, "]");
        assert_eq!(frame.cells[78].symbol, " ");
        assert!(frame.cells[67..79]
            .iter()
            .all(|cell| !cell.skip && cell.hyperlink.is_none()));
        assert_eq!(hits.len(), 1);
    }

    #[test]
    #[ignore = "manual fixed-geometry render scaling profile"]
    fn results_buttons_profile_one_and_fifteen_panes() {
        let palette = Palette::catppuccin();
        for count in [1, 15] {
            let (mut frame, snapshot, panes) = fixture(count);
            let mut hits = Vec::with_capacity(count);
            let started = std::time::Instant::now();
            for _ in 0..10_000 {
                render_results_buttons(
                    std::hint::black_box(&mut frame),
                    &snapshot,
                    &panes,
                    &palette,
                    &mut hits,
                );
                std::hint::black_box(&hits);
            }
            eprintln!(
                "results_buttons: {count} panes, fixed 240x80, {:?}/render",
                started.elapsed() / 10_000
            );
        }
    }
}
