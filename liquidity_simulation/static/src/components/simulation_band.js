/** @odoo-module **/
/*
 * Simulation band.
 *
 * Draws the Monte Carlo result as a band around the deterministic baseline:
 *
 *   P10-P90   shaded area       the range nine runs in ten land inside
 *   P50       solid midline     the median outcome, not a promise
 *   baseline  thin dotted line  the deterministic curve, for comparison
 *   events    markers           tax/payroll/loan days, so a dip is explained
 *
 * Why a custom component and not the standard graph view: Odoo's graph has no
 * hook for shading an area between two series or for marking the zero line.
 * Faking it with a second series would draw a line, not a band, and would
 * still not explain the dips.
 *
 * The component renders nothing at all when there are no lines. An empty band
 * frame is worse than no band: it looks like a result.
 *
 * Everything below is rendered by the template, not by innerHTML: the SVG
 * nodes are computed into plain arrays of {x, y} and the template loops over
 * them. Writing markup into the DOM by hand would bypass OWL's patching, so
 * a re-render would either duplicate the drawing or leave a stale one behind.
 */
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const PADDING = { top: 24, right: 16, bottom: 34, left: 72 };
const HEIGHT = 320;
const FALLBACK_WIDTH = 900;

export class SimulationBand extends Component {
    static template = "liquidity_simulation.SimulationBand";
    static props = {
        record: { type: Object, optional: true },
        "*": true,
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            lines: [],
            contributions: [],
            loading: true,
            selectedDate: null,
        });
        onWillStart(async () => {
            await this.load();
        });
    }

    get simulationId() {
        return this.props.record?.resId;
    }

    async load() {
        const id = this.simulationId;
        if (!id) {
            this.state.lines = [];
            this.state.contributions = [];
            this.state.loading = false;
            return;
        }
        const [lines, contributions] = await Promise.all([
            this.orm.searchRead(
                "liquidity.simulation.line",
                [["simulation_id", "=", id]],
                [
                    "date",
                    "p10",
                    "p50",
                    "p90",
                    "p_negative",
                    "baseline_balance",
                    "expected_events",
                ],
                { order: "date asc" }
            ),
            this.orm.searchRead(
                "liquidity.variance.contribution",
                [["simulation_id", "=", id]],
                ["label", "source_type", "variance_share"],
                { order: "variance_share desc", limit: 10 }
            ),
        ]);
        this.state.lines = lines;
        this.state.contributions = contributions;
        this.state.loading = false;
    }

    get hasBand() {
        return this.state.lines.length > 0;
    }

    /**
     * Geometry as plain data. Kept out of the template so the arithmetic is
     * readable on its own and the template stays a loop.
     */
    get geometry() {
        const lines = this.state.lines;
        if (!lines.length) {
            return null;
        }
        const width = FALLBACK_WIDTH;
        const innerW = width - PADDING.left - PADDING.right;
        const innerH = HEIGHT - PADDING.top - PADDING.bottom;

        const values = [0];
        for (const line of lines) {
            for (const key of ["p10", "p50", "p90", "baseline_balance"]) {
                const value = line[key];
                if (value !== null && value !== undefined) {
                    values.push(value);
                }
            }
        }
        // Zero is always on screen: the point of the band is to show how
        // close to zero it comes.
        const min = Math.min(...values);
        const max = Math.max(...values);
        const span = max - min || 1;

        const xAt = (index) =>
            PADDING.left +
            (lines.length === 1 ? innerW / 2 : (index / (lines.length - 1)) * innerW);
        const yAt = (value) => PADDING.top + innerH - ((value - min) / span) * innerH;

        const upper = lines.map((line, index) => ({
            x: xAt(index),
            y: yAt(line.p90),
        }));
        const lower = lines
            .map((line, index) => ({ x: xAt(index), y: yAt(line.p10) }))
            .reverse();
        const bandPath =
            upper.map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`).join(" ") +
            " " +
            lower.map((p) => `L${p.x},${p.y}`).join(" ") +
            " Z";

        const medianPath = lines
            .map((line, index) => `${index === 0 ? "M" : "L"}${xAt(index)},${yAt(line.p50)}`)
            .join(" ");

        const baselinePoints = [];
        lines.forEach((line, index) => {
            const value = line.baseline_balance;
            if (value === null || value === undefined) {
                return;
            }
            baselinePoints.push({ x: xAt(index), y: yAt(value) });
        });
        const baselinePath = baselinePoints
            .map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`)
            .join(" ");

        const zeroY = yAt(0);
        const negativeHeight = Math.max(0, PADDING.top + innerH - zeroY);

        // One marker per event day. Several event types on the same day are
        // joined into one label -- two labels on the same x would overlap and
        // neither would be readable.
        const events = [];
        lines.forEach((line, index) => {
            if (!line.expected_events) {
                return;
            }
            events.push({
                key: `event-${line.date}`,
                x: xAt(index),
                label: line.expected_events,
            });
        });

        // A hover/click target per day, wide enough to hit with a mouse.
        const step = lines.length > 1 ? innerW / (lines.length - 1) : innerW;
        const hitAreas = lines.map((line, index) => ({
            key: `hit-${line.date}`,
            date: line.date,
            x: xAt(index) - step / 2,
            width: Math.max(4, step),
        }));

        return {
            width,
            height: HEIGHT,
            innerW,
            innerH,
            bandPath,
            medianPath,
            baselinePath,
            zeroY,
            negativeHeight,
            events,
            hitAreas,
            firstDate: lines[0].date,
            lastDate: lines[lines.length - 1].date,
        };
    }

    get selectedLine() {
        const date = this.state.selectedDate;
        if (!date) {
            return null;
        }
        return this.state.lines.find((line) => line.date === date) || null;
    }

    get selectedContributions() {
        if (!this.state.selectedDate) {
            return [];
        }
        return this.state.contributions;
    }

    onDayClick(date) {
        // Clicking the same day again closes the panel: a detail view you
        // cannot dismiss is a detail view that covers the band.
        this.state.selectedDate = this.state.selectedDate === date ? null : date;
    }

    formatPercent(value) {
        if (value === null || value === undefined) {
            return "—";
        }
        return `${(value * 100).toFixed(1)} %`;
    }
}

export const simulationBandField = {
    component: SimulationBand,
    supportedTypes: ["one2many"],
    extractProps: ({ record }) => ({ record }),
};

registry.category("fields").add("liquidity_simulation_band", simulationBandField);
