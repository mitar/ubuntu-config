import Gio from 'gi://Gio';

// The fields below come from struct gpu_metrics_v3_0 in drivers/gpu/drm/amd/include/kgd_pp_interface.h, at the
// offsets the kernel compiles that struct to with its natural alignment. amdgpu publishes the struct as a whole and
// describes it only by the revision and size in its header, so reading a field means taking it from a fixed offset
// once the header says the layout is the one these offsets belong to.
const STRUCTURE_SIZE = 264;
const FORMAT_REVISION = 3;

const OFFSET_STRUCTURE_SIZE = 0;
const OFFSET_FORMAT_REVISION = 2;
const OFFSET_TEMPERATURE_GFX = 4;
const OFFSET_TEMPERATURE_SOC = 6;
const OFFSET_TEMPERATURE_CORE = 8;
const OFFSET_TEMPERATURE_SKIN = 40;
const OFFSET_AVERAGE_SOCKET_POWER = 112;

const TEMPERATURE_CORE_COUNT = 16;

// A field which holds the largest value it can is how the table says it carries nothing, for entries the firmware
// leaves out.
const UNSET_UINT16 = 0xffff;

// Counters wrap at the range of the unsigned 32 bit fields that hold them, after about 49.7 days of uptime.
const COUNTER_MODULUS = 2 ** 32;

/**
 * The reasons the processor can be held below the speed it would otherwise run at, in the order the kernel declares
 * their counters. SPL is the sustained power limit, and FPPT and SPPT the fast and slow package power tracking
 * limits, which together keep the package within its power budget over different time scales. The THM reasons are
 * the temperature limits of the individual domains. PROCHOT is asserted by the platform rather than by the
 * processor, and the platform does not report what made it assert it, so it is its own kind.
 *
 * Each entry carries the offset of its throttle_residency_ field, which counts the milliseconds the processor has
 * spent limited by that reason since boot.
 */
export const REASONS = [
    {key: 'prochot', offset: 228, short: 'PROCHOT', label: 'Platform (PROCHOT)', kind: 'platform'},
    {key: 'spl', offset: 232, short: 'SPL', label: 'Sustained power (SPL)', kind: 'power'},
    {key: 'fppt', offset: 236, short: 'FPPT', label: 'Fast power (FPPT)', kind: 'power'},
    {key: 'sppt', offset: 240, short: 'SPPT', label: 'Slow power (SPPT)', kind: 'power'},
    {key: 'thm_core', offset: 244, short: 'THM', label: 'Core temperature', kind: 'thermal'},
    {key: 'thm_gfx', offset: 248, short: 'THM', label: 'Graphics temperature', kind: 'thermal'},
    {key: 'thm_soc', offset: 252, short: 'THM', label: 'SoC temperature', kind: 'thermal'},
];

/**
 * Decodes a metrics table.
 *
 * @param {Uint8Array} bytes - the contents of a gpu_metrics file
 * @returns {object|null} the temperatures in degrees Celsius, the package power in watts, and the residency counter
 *   of every reason keyed by its name, or null when the contents are not a table in the layout this decodes
 */
export function decodeMetrics(bytes) {
    if (bytes.length !== STRUCTURE_SIZE)
        return null;
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    // structure_size is the size of the table the kernel filled in, so together with the revision it tells the
    // layout apart from the ones other generations publish through the same file.
    if (view.getUint8(OFFSET_FORMAT_REVISION) !== FORMAT_REVISION ||
        view.getUint16(OFFSET_STRUCTURE_SIZE, true) !== STRUCTURE_SIZE)
        return null;

    // The core temperature limit acts on the hottest core, so that is the one worth reporting next to its counter.
    let temperatureCore = 0;
    for (let i = 0; i < TEMPERATURE_CORE_COUNT; i++) {
        const value = view.getUint16(OFFSET_TEMPERATURE_CORE + 2 * i, true);
        if (value !== UNSET_UINT16)
            temperatureCore = Math.max(temperatureCore, value);
    }

    const residency = {};
    for (const reason of REASONS)
        residency[reason.key] = view.getUint32(reason.offset, true);

    // Temperatures are in hundredths of a degree Celsius and power is in milliwatts.
    return {
        temperatureCore: temperatureCore / 100,
        temperatureGfx: view.getUint16(OFFSET_TEMPERATURE_GFX, true) / 100,
        temperatureSoc: view.getUint16(OFFSET_TEMPERATURE_SOC, true) / 100,
        temperatureSkin: view.getUint16(OFFSET_TEMPERATURE_SKIN, true) / 100,
        socketPower: view.getUint32(OFFSET_AVERAGE_SOCKET_POWER, true) / 1000,
        residency,
    };
}

/**
 * Finds the file through which a device publishes a metrics table this can decode.
 *
 * @returns {Gio.File|null} the file, or null when no device on this system publishes such a table
 */
export function findMetricsFile() {
    // Which index a card gets depends on the order the devices came up, so it is not stable across boots and every
    // card has to be tried. A card of a generation that publishes a different layout fails to decode and is skipped.
    for (const card of listCards()) {
        const file = Gio.File.new_for_path(`/sys/class/drm/${card}/device/gpu_metrics`);
        try {
            const [, contents] = file.load_contents(null);
            if (decodeMetrics(contents))
                return file;
        } catch {
            // A card without the file at all, which is the usual case for anything but the render device.
        }
    }
    return null;
}

/**
 * Computes how much of the time between two readings the processor spent held back by each reason.
 *
 * @param {object} previous - the earlier reading, as decodeMetrics returned it
 * @param {object} current - the later reading, as decodeMetrics returned it
 * @param {number} elapsed - the milliseconds between the two readings, which has to be above zero
 * @returns {object} the percentage of that time attributed to each reason, keyed by its name
 */
export function throttleShares(previous, current, elapsed) {
    const shares = {};
    for (const reason of REASONS) {
        const grown = (current.residency[reason.key] - previous.residency[reason.key] + COUNTER_MODULUS) %
            COUNTER_MODULUS;
        // Rounding inside the SMU can attribute slightly more than the whole interval to a reason.
        shares[reason.key] = Math.min(100, 100 * grown / elapsed);
    }
    return shares;
}

/**
 * Lists the names of the DRM cards on this system.
 *
 * @returns {string[]} the card names, or an empty array when they cannot be listed
 */
function listCards() {
    const cards = [];
    try {
        const directory = Gio.File.new_for_path('/sys/class/drm');
        const children = directory.enumerate_children('standard::name', Gio.FileQueryInfoFlags.NONE, null);
        let child;
        while ((child = children.next_file(null)) !== null) {
            // The connectors and the render nodes sit in the same directory and carry the card name as a prefix.
            if (/^card\d+$/.test(child.get_name()))
                cards.push(child.get_name());
        }
        children.close(null);
    } catch {
        // A system without the DRM subsystem present, where there is nothing to find.
    }
    return cards;
}

/**
 * Picks the reason to name as the one holding the processor back.
 *
 * @param {object} shares - the percentages throttleShares returned
 * @param {number} threshold - the percentage a reason has to reach before it counts as holding anything back
 * @returns {object|null} the entry of REASONS with the largest share, or null when none of them reaches the
 *   threshold, which is to say that nothing is holding the processor back
 */
export function dominantReason(shares, threshold) {
    // The largest share is the limit that was binding for most of the interval. REASONS is in the order the kernel
    // declares the counters, and an exact tie goes to the first of them.
    let dominant = null;
    for (const reason of REASONS) {
        if (shares[reason.key] >= threshold && (!dominant || shares[reason.key] > shares[dominant.key]))
            dominant = reason;
    }
    return dominant;
}
