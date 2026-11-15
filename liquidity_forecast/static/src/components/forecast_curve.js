/** @odoo-module **/
/*
 * Forecast curve.
 *
 * The standard graph view draws the line but has no hook for the two things
 * that matter here: marking the zero line and shading the area below it. A
 * graph that merely crosses zero leaves the reader to notice the crossing
 * themselves, on an axis whose scale they have to work out first. In a
 * liquidity forecast the crossing *is* the event — so it is drawn.
 *
 *   closing balance  solid line     one point per day of the horizon
 *   zero line        heavy rule     where the cash runs out
 *   below zero       red shading    the days the company cannot pay
 *   events           dashed marker  tax/payroll/loan days, when they exist
 *
 * The component renders nothing when there are no lines: an empty frame looks
 * like a result.
 *
 * Geometry is computed into plain arrays and the template loops over them.
 * Writing SVG markup by hand would bypass OWL's patching, so a re-render
 * would either duplicate the drawing or leave a stale one behind.
 */
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const PADDING = { top: 24, right: 16, bottom: 34, left: 72 };
const HEIGHT = 320;
const FALLBACK_WIDTH = 900;

export class ForecastCurve extends Component {
    static template = "liquidity_forecast.ForecastCurve";
    static props = {
        record: { type: Object, optional: true },
        "*": true,
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            lines: [],
            loading: true,
            selectedDate: null,
        });
        onWillStart(async () => {
            await this.load();
        });
    }

    get forecastId() {
        return this.props.record?.resId;
    }

    async load() {
        const id = this.forecastId;
        if (!id) {
            this.state.lines = [];
            this.state.loading = false;
            return;
        }
        this.state.lines = await this.orm.searchRead(
            "liquidity.forecast.line",
            [["forecast_id", "=", id]],
            [
                "date",
                "opening_balance",
                "incoming",
                "outgoing",
                "closing_balance",
                "has_event",
                "event_type",
            ],
            { order: "date asc" }
        );
        this.state.loading = false;
    }

    get hasCurve() {
        return this.state.lines.length > 0;
    }

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
            if (line.closing_balance !== null && line.closing_balance !== undefined) {
                values.push(line.closing_balance);
            }
        }
        // Zero is always in range: the point of the curve is to show whether
        // and when it crosses.
        const min = Math.min(...values);
        const max = Math.max(...values);
        const span = max - min || 1;

        const xAt = (index) =>
            PADDING.left +
            (lines.length === 1 ? innerW / 2 : (index / (lines.length - 1)) * innerW);
        const yAt = (value) => PADDING.top + innerH - ((value - min) / span) * innerH;

        const curvePath = lines
            .map(
                (line, index) =>
                    `${index === 0 ? "M" : "L"}${xAt(index)},${yAt(line.closing_balance)}`
            )
            .join(" ");

        const zeroY = yAt(0);
        const negativeHeight = Math.max(0, PADDING.top + innerH - zeroY);

        // The days the balance is below zero, so the shading can be described
        // in words and not only in colour -- colour alone is not accessible.
        const negativeDays = lines.filter(
            (line) => line.closing_balance !== null && line.closing_balance < 0
        );

        const events = [];
        lines.forEach((line, index) => {
            if (!line.has_event) {
                return;
            }
            events.push({
                key: `event-${line.date}`,
                x: xAt(index),
                label: line.event_type || "",
            });
        });

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
            curvePath,
            zeroY,
            negativeHeight,
            negativeDays,
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

    onDayClick(date) {
        this.state.selectedDate = this.state.selectedDate === date ? null : date;
    }
}

export const forecastCurveField = {
    component: ForecastCurve,
    supportedTypes: ["one2many"],
    extractProps: ({ record }) => ({ record }),
};

registry.category("fields").add("liquidity_forecast_curve", forecastCurveField);
