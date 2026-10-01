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
const OFFSET_AVERAGE_GFX_POWER = 124;
const OFFSET_AVERAGE_ALL_CORE_POWER = 132;
const OFFSET_AVERAGE_CORE_POWER = 136;
const OFFSET_CURRENT_CORECLK = 190;
const OFFSET_AVERAGE_SOCKET_POWER = 112;

/**
 * Where each core sits in the per-core arrays, and the frequency it reaches when nothing holds it back.
 *
 * The table carries no mapping of its own, so this was measured by pinning a load to one core at a time and
 * watching which entry of average_core_power responded: on this processor the four classic cores occupy the even
 * entries 0 to 6 and the eight dense cores the entries 8 to 15, leaving 1, 3, 5 and 7 unused. A processor with a
 * different split may lay them out differently, in which case the activity test below keeps the result harmless
 * rather than wrong, since a core which is not where this expects it reads as idle instead of as a bad number.
 */
export const CORE_GROUPS = [
    {name: 'Zen 5', slots: [0, 2, 4, 6], maxFrequency: 5157},
    {name: 'Zen 5c', slots: [8, 9, 10, 11, 12, 13, 14, 15], maxFrequency: 3289},
];

// A core drawing less than this is idle rather than held back, and a percentage of its maximum would then say
// something about how little work it has rather than about throttling. Loaded cores draw several watts and idle
// ones well under a tenth of one, so anything in between separates them.
const ACTIVE_CORE_POWER_W = 0.5;

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
    // Only the entries which belong to a core are considered, because the unused ones carry a value as well and
    // it has nothing to do with any core.
    let temperatureCore = 0;
    for (const group of CORE_GROUPS) {
        for (const slot of group.slots) {
            const value = view.getUint16(OFFSET_TEMPERATURE_CORE + 2 * slot, true);
            if (value !== UNSET_UINT16)
                temperatureCore = Math.max(temperatureCore, value);
        }
    }

    // A group runs at one clock per core, and the limit acts on whichever of them is working hardest, so the
    // busiest core of each group is the one whose clock says how much of its speed the group is being allowed.
    const coreGroups = CORE_GROUPS.map(group => {
        let busiest = group.slots[0];
        let busiestPower = 0;
        for (const slot of group.slots) {
            const power = view.getUint16(OFFSET_AVERAGE_CORE_POWER + 2 * slot, true) / 1000;
            if (power > busiestPower) {
                busiestPower = power;
                busiest = slot;
            }
        }
        const active = busiestPower >= ACTIVE_CORE_POWER_W;
        return {
            name: group.name,
            maxFrequency: group.maxFrequency,
            active,
            frequency: active ? view.getUint16(OFFSET_CURRENT_CORECLK + 2 * busiest, true) : 0,
        };
    });

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
        corePower: view.getUint32(OFFSET_AVERAGE_ALL_CORE_POWER, true) / 1000,
        graphicsPower: view.getUint32(OFFSET_AVERAGE_GFX_POWER, true) / 1000,
        coreGroups,
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
