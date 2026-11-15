/** @odoo-module **/
/*
 * Simulation band.
 *
 * Draws the Monte Carlo result as a band around the deterministic baseline:
 *
 *   P10-P90   shaded area      the range nine runs in ten land inside
 *   P50       solid midline    the median outcome, not a promise
 *   baseline  thin dotted line the deterministic curve, for comparison
 *   events    markers          tax/payroll/loan days, so a dip is explained
 *
 * Why a custom component and not the standard graph view: Odoo's graph has no
 * hook for shading an area between two series or for marking the zero line.
 * Faking it with a second series would draw a line, not a band, and would
 * still not explain the dips.
 *
 * The component renders nothing at all when there are no lines. An empty band
 * frame is worse than no band: it looks like a result.
 */
import { Component, onWillStart, useRef, onMounted, onWillUpdateProps } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const SVG_NS = "http://www.w3.org/2000/svg";
const PADDING = { top: 24, right: 16, bottom: 34, left: 72 };

export class SimulationBand extends Component {
    static template = "liquidity_simulation.SimulationBand";
    static props = {
        record: { type: Object, optional: true },
        "*": true,
    };

    setup() {
        this.svgRef = useRef("svg");
        this.orm = useService("orm");
        this.state = { lines: [], loading: true };
        onWillStart(async () => {
            await this.loadLines();
        });
        onWillUpdateProps(async (nextProps) => {
            if (nextProps.record?.resId !== this.props.record?.resId) {
                await this.loadLines();
            }
        });
        onMounted(() => this.render());
    }

    get simulationId() {
        return this.props.record?.resId;
    }

    async loadLines() {
        const id = this.simulationId;
        if (!id) {
            this.state = { lines: [], loading: false };
            return;
        }
        const lines = await this.orm.searchRead(
            "liquidity.simulation.line",
            [["simulation_id", "=", id]],
            ["date", "p10", "p50", "p90", "p_negative", "baseline_balance", "expected_events"],
            { order: "date asc" }
        );
        this.state = { lines, loading: false };
        if (this.svgRef.el) {
            this.render();
        }
    }

    get hasBand() {
        return this.state.lines.length > 0;
    }

    /** Build the geometry once, then paint. Kept out of the template so the
     *  maths is testable and the template stays readable. */
    buildGeometry() {
        const lines = this.state.lines;
        const width = this.svgRef.el?.clientWidth || 900;
        const height = 320;
        const innerW = Math.max(10, width - PADDING.left - PADDING.right);
        const innerH = Math.max(10, height - PADDING.top - PADDING.bottom);

        const values = [];
        for (const line of lines) {
            for (const key of ["p10", "p50", "p90", "baseline_balance"]) {
                if (line[key] !== null && line[key] !== undefined) {
                    values.push(line[key]);
                }
            }
        }
        // Always include zero so the zero line is on screen: the point of the
        // band is to show how close to zero it comes.
        values.push(0);
        const min = Math.min(...values);
        const max = Math.max(...values);
        const span = max - min || 1;

        const x = (index) =>
            PADDING.left + (lines.length === 1 ? innerW / 2 : (index / (lines.length - 1)) * innerW);
        const y = (value) => PADDING.top + innerH - ((value - min) / span) * innerH;

        const upper = lines.map((l, i) => [x(i), y(l.p90)]);
        const lower = lines.map((l, i) => [x(i), y(l.p10)]).reverse();
        const bandPath =
            upper.map((p, i) => `${i === 0 ? "M" : "L"}${p[0]},${p[1]}`).join(" ") +
            " " +
            lower.map((p) => `L${p[0]},${p[1]}`).join(" ") +
            " Z";

        const medianPath = lines
            .map((l, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(l.p50)}`)
            .join(" ");

        const baselinePath = lines
            .map((l, i) => {
                const value = l.baseline_balance;
                if (value === null || value === undefined) {
                    return "";
                }
                return `${i === 0 ? "M" : "L"}${x(i)},${y(value)}`;
            })
            .filter(Boolean)
            .join(" ");

        const zeroY = y(0);
        const negativeTop = zeroY;
        const negativeHeight = PADDING.top + innerH - zeroY;

        const events = lines
            .map((l, i) => ({ line: l, index: i }))
            .filter(({ line }) => line.expected_events);

        return {
            width,
            height,
            innerW,
            innerH,
            bandPath,
            medianPath,
            baselinePath,
            zeroY,
            negativeTop,
            negativeHeight: Math.max(0, negativeHeight),
            events,
            x,
            y,
            lines,
        };
    }

    render() {
        const el = this.svgRef.el;
        if (!el || !this.hasBand) {
            return;
        }
        const geometry = this.buildGeometry();
        el.innerHTML = "";
        el.setAttribute("viewBox", `0 0 ${geometry.width} ${geometry.height}`);
        el.setAttribute("width", "100%");
        el.setAttribute("height", String(geometry.height));

        const make = (tag, attrs = {}, text = null) => {
            const node = document.createElementNS(SVG_NS, tag);
            for (const [key, value] of Object.entries(attrs)) {
                node.setAttribute(key, value);
            }
            if (text !== null) {
                node.textContent = text;
            }
            el.appendChild(node);
            return node;
        };

        // Negative area, so "below zero" is visible without reading numbers.
        if (geometry.negativeHeight > 0) {
            make("rect", {
                x: PADDING.left,
                y: geometry.negativeTop,
                width: geometry.innerW,
                height: geometry.negativeHeight,
                class: "o_liquidity_band_negative",
            });
        }

        make("path", { d: geometry.bandPath, class: "o_liquidity_band_area" });
        make("path", { d: geometry.medianPath, class: "o_liquidity_band_median" });
        if (geometry.baselinePath) {
            make("path", { d: geometry.baselinePath, class: "o_liquidity_band_baseline" });
        }

        make("line", {
            x1: PADDING.left,
            x2: PADDING.left + geometry.innerW,
            y1: geometry.zeroY,
            y2: geometry.zeroY,
            class: "o_liquidity_band_zero",
        });
        make(
            "text",
            { x: PADDING.left - 8, y: geometry.zeroY + 4, class: "o_liquidity_band_axis", "text-anchor": "end" },
            "0"
        );

        // Event markers: a dip in the band gets a visible cause.
        for (const { line, index } of geometry.events) {
            const px = geometry.x(index);
            make("line", {
                x1: px,
                x2: px,
                y1: PADDING.top,
                y2: PADDING.top + geometry.innerH,
                class: "o_liquidity_band_event",
            });
            make(
                "text",
                {
                    x: px + 3,
                    y: PADDING.top + 12,
                    class: "o_liquidity_band_event_label",
                },
                line.expected_events
            );
        }

        // First and last date, so the axis is not anonymous.
        const first = geometry.lines[0];
        const last = geometry.lines[geometry.lines.length - 1];
        make(
            "text",
            { x: PADDING.left, y: geometry.height - 10, class: "o_liquidity_band_axis" },
            first.date
        );
        make(
            "text",
            {
                x: PADDING.left + geometry.innerW,
                y: geometry.height - 10,
                class: "o_liquidity_band_axis",
                "text-anchor": "end",
            },
            last.date
        );
    }
}

SimulationBand.template = "liquidity_simulation.SimulationBand";

export const simulationBandField = {
    component: SimulationBand,
    supportedTypes: ["one2many"],
    extractProps: ({ record }) => ({ record }),
};

registry.category("fields").add("liquidity_simulation_band", simulationBandField);
