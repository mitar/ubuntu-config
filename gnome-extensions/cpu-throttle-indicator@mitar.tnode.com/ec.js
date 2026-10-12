import Gio from 'gi://Gio';

// The hwmon device the cros_ec driver registers for the embedded controller, and the label Framework's EC gives the
// sensor through which it reads the APU's own temperature over SB-TSI. The EC runs the fan from its sensors, and this
// is the only one of them which follows the APU rather than the board around it.
const HWMON_NAME = 'cros_ec';
const APU_SENSOR_LABEL = 'cpu@4c';

/**
 * Finds the file through which the embedded controller reports the temperature it reads from the APU.
 *
 * The file holds the temperature in millidegrees Celsius. Reading it fails with Gio.IOErrorEnum.INVALID_DATA while the
 * EC cannot read the sensor, because the driver answers every error state the EC reports for a sensor with ENODATA.
 *
 * @returns {Gio.File|null} the file, or null when no embedded controller on this system reports such a sensor
 */
export function findApuSensorFile() {
    // Which index a hwmon device gets depends on the order the drivers came up, so it is not stable across boots, and
    // the index of a sensor is the EC's own numbering, so both are found by name.
    for (const device of listChildren('/sys/class/hwmon')) {
        const directory = `/sys/class/hwmon/${device}`;
        if (readText(`${directory}/name`) !== HWMON_NAME)
            continue;
        for (const name of listChildren(directory)) {
            const match = /^(temp\d+)_label$/.exec(name);
            if (match && readText(`${directory}/${name}`) === APU_SENSOR_LABEL)
                return Gio.File.new_for_path(`${directory}/${match[1]}_input`);
        }
    }
    return null;
}

/**
 * Lists the names of the entries of a directory.
 *
 * @param {string} path - the directory
 * @returns {string[]} the names, or an empty array when the directory cannot be listed
 */
function listChildren(path) {
    const names = [];
    try {
        const children = Gio.File.new_for_path(path).enumerate_children('standard::name', Gio.FileQueryInfoFlags.NONE,
            null);
        let child;
        while ((child = children.next_file(null)) !== null)
            names.push(child.get_name());
        children.close(null);
    } catch {
        // A system without the directory, where there is nothing to find.
    }
    return names;
}

/**
 * Reads a sysfs attribute which holds a line of text.
 *
 * @param {string} path - the attribute
 * @returns {string|null} the text without its trailing newline, or null when the attribute cannot be read
 */
function readText(path) {
    try {
        const [, contents] = Gio.File.new_for_path(path).load_contents(null);
        return new TextDecoder().decode(contents).trim();
    } catch {
        return null;
    }
}
