# Hardware, Electronics, and Control

**Project:** usphere-DAQ
**Document Type:** Hardware & Control Reference
**Date:** February 17, 2026
**Status:** Draft - Template for expansion

---

## 1. Overview

This document catalogs the hardware, electronics, and control systems used in the optically levitated microsphere experiment. It serves as a reference for the physical setup, signal chain, and control architecture.

---

## 2. Laser Systems

### 2.1 Trapping Laser
| Parameter | Value |
|-----------|-------|
| Wavelength | <!-- e.g., 1064 nm --> |
| Model | <!-- --> |
| Max power | <!-- --> |
| Operating power | <!-- --> |
| Polarization | <!-- e.g., circular or linear --> |
| Beam delivery | <!-- e.g., single-mode fiber --> |

### 2.2 Imaging Lasers
| Parameter | Value |
|-----------|-------|
| Wavelength | <!-- e.g., 532 nm --> |
| Model | <!-- --> |
| Number of beams | <!-- e.g., 2 (vertical and horizontal) --> |
| Purpose | Position detection in 3 DOF |

---

## 3. Optics

### 3.1 Trapping Optics
- **Focusing lens / objective:** <!-- NA, focal length, working distance (~75 mm) -->
- **Fiber launch / enclosure:** <!-- Sealed enclosure for pointing noise reduction, pumped to < 0.1 mbar -->
- **Acousto-optic modulator (AOM):** <!-- Model, purpose: Z-axis feedback via power modulation -->
- **Piezo deflection mirror:** <!-- Model, bandwidth (~1 kHz), purpose: X/Y beam steering for feedback -->

### 3.2 Imaging Optics
- **Beam splitters:** <!-- e.g., D-shaped pickoff mirror for X, sharp-edged mirrors for Y/Z -->
- **Lenses / objectives:** <!-- Imaging optics at vacuum chamber output -->
- **Beam waist at sphere:** <!-- Much larger than sphere size for full illumination -->

### 3.3 Other Optical Components
- **Harmonic beam splitter:** <!-- -->
- **Mirrors, mounts, and alignment hardware:** <!-- -->
- **Optical fibers and couplers:** <!-- -->

---

## 4. Vacuum System

### 4.1 Vacuum Chamber
- **Chamber type / model:** <!-- -->
- **Window configuration:** <!-- Number, material, positions -->
- **Feedthroughs:** <!-- Electrical, optical, mechanical -->

### 4.2 Pumps
| Pump | Type | Model | Pressure Range | Notes |
|------|------|-------|---------------|-------|
| Roughing | <!-- e.g., scroll, diaphragm --> | <!-- --> | Atmosphere to ~10^-2 mbar | <!-- --> |
| Turbo | <!-- --> | <!-- --> | 10^-2 to 10^-7 mbar | <!-- --> |
| Ion / getter | <!-- if applicable --> | <!-- --> | < 10^-7 mbar | <!-- --> |

### 4.3 Pressure Gauges
| Gauge | Model | Range | Location |
|-------|-------|-------|----------|
| <!-- --> | <!-- --> | <!-- --> | <!-- --> |

### 4.4 Valves and Plumbing
<!-- Gate valves, leak valves, gas inlet for loading, etc. -->

---

## 5. Electrodes and Electric Field Control

### 5.1 Electrode Geometry
- **Configuration:** <!-- e.g., 6 planar electrodes surrounding trap, parallel plate -->
- **Electrode diameter:** <!-- e.g., 25.4 mm -->
- **Material:** <!-- e.g., gold-coated -->
- **Separation:** <!-- e.g., controllable 1-20 mm, typical ~3.3-4.0 mm -->
- **Mounting:** <!-- e.g., vacuum-compatible translation stage -->
- **Grounding scheme:** <!-- e.g., lower electrode grounded, upper 5 independently biased -->

### 5.2 High Voltage Amplifier
- **Model:** <!-- e.g., Trek 2220 -->
- **Output range:** <!-- -->
- **Bandwidth:** <!-- -->
- **Purpose:** Calibration impulses, charge management, E-field generation

### 5.3 Signal Generator / Waveform Source
<!-- For driving oscillating E-fields for charge measurement, calibration pulses -->

---

## 6. Detectors and Sensors

### 6.1 Photodetectors
| Detector | Type | Model | DOF Measured | Role |
|----------|------|-------|-------------|------|
| X sensor | <!-- e.g., balanced photodiode (BPD) --> | <!-- --> | X (radial) | In-loop |
| Y sensor | <!-- e.g., lateral effect position sensor (LSP) --> | <!-- --> | Y (radial) | In-loop |
| Z sensor | <!-- e.g., LSP --> | <!-- --> | Z (vertical) | In-loop |
| Out-of-loop | <!-- e.g., BPD --> | <!-- --> | X | Science readout |

### 6.2 Accelerometer
- **Model:** <!-- e.g., Wilcoxon 731A/P31 -->
- **Location:** <!-- e.g., mounted on vacuum chamber exterior -->
- **Purpose:** Environmental vibration monitoring, data quality cuts

### 6.3 Other Sensors
<!-- Temperature sensors, laser power monitors, etc. -->

---

## 7. Charge Management Hardware

### 7.1 UV Light Source
- **Type:** <!-- e.g., xenon flash lamp -->
- **Model:** <!-- -->
- **Purpose:** Photoelectric charging/discharging of sphere and electrodes
- **Mounting location:** <!-- -->

### 7.2 Electron Source (if applicable)
- **Type:** <!-- e.g., tungsten filament for thermionic emission -->
- **Purpose:** Adding electrons to the sphere

---

## 8. FPGA and Digital Control

### 8.1 FPGA
- **Model:** <!-- -->
- **Firmware / gateware:** <!-- -->
- **Clock rate:** <!-- -->

### 8.2 FPGA Functions
| Function | Description |
|----------|-------------|
| Feedback control | Reads sensor signals, computes and outputs feedback for X, Y, Z |
| AOM drive | Modulates trapping beam power for Z feedback |
| Piezo drive | Steers trapping beam for X, Y feedback |
| Data streaming | <!-- If FPGA handles data output to DAQ --> |
| Calibration pulses | <!-- If FPGA triggers calibration sequences --> |

### 8.3 FPGA I/O
| Channel | Direction | Signal | Connected To |
|---------|-----------|--------|-------------|
| <!-- --> | Input | <!-- --> | <!-- --> |
| <!-- --> | Output | <!-- --> | <!-- --> |

---

## 9. Data Acquisition Hardware

### 9.1 Digitizer / ADC
- **Model:** <!-- -->
- **Number of channels:** <!-- -->
- **Sampling rate:** <!-- e.g., 10 kHz -->
- **Resolution:** <!-- e.g., 16-bit -->
- **Interface:** <!-- e.g., PCIe, USB, Ethernet -->

### 9.2 DAQ Computer
- **OS:** <!-- -->
- **DAQ software:** <!-- -->
- **Storage:** <!-- -->

### 9.3 Recorded Channels
| Channel | Signal | Sensor | Sampling Rate | Notes |
|---------|--------|--------|--------------|-------|
| <!-- --> | X position (in-loop) | BPD | <!-- --> | <!-- --> |
| <!-- --> | X position (out-of-loop) | BPD | <!-- --> | <!-- --> |
| <!-- --> | Y position | LSP | <!-- --> | <!-- --> |
| <!-- --> | Z position | LSP | <!-- --> | <!-- --> |
| <!-- --> | Accelerometer | Wilcoxon | <!-- --> | <!-- --> |
| <!-- --> | <!-- other signals --> | <!-- --> | <!-- --> | <!-- --> |

---

## 10. Control Architecture

### 10.1 Signal Flow Diagram
```
Imaging Lasers (532 nm) --> Sphere --> Photodetectors (BPD, LSP)
                                            |
                                            v
                                         FPGA
                                       /       \
                                      v         v
                                    AOM      Piezo Mirror
                                     |           |
                                     v           v
                              Z feedback    X,Y feedback
                            (power mod)   (beam steering)
```

### 10.2 Feedback Loop Parameters
| DOF | Sensor | Actuator | Bandwidth | Feedback Type |
|-----|--------|----------|-----------|---------------|
| X | <!-- --> | Piezo mirror | <!-- e.g., ~1 kHz --> | <!-- e.g., proportional --> |
| Y | <!-- --> | Piezo mirror | <!-- --> | <!-- --> |
| Z | <!-- --> | AOM | <!-- --> | <!-- e.g., PID --> |

### 10.3 Control Software
<!-- Describe any software used for experiment control, parameter setting, automation -->

---

## 11. Microsphere Loading Hardware

- **Loading mechanism:** <!-- e.g., vibrating glass slide, nebulizer -->
- **Sphere dispensing:** <!-- How spheres are placed on the slide -->
- **Loading pressure:** <!-- e.g., ~1 mbar -->

---

## 12. Wiring and Cabling Diagram

<!-- Describe or reference a diagram showing the physical signal routing: which cables connect which devices, BNC/SMA/fiber runs, etc. -->

---

## 13. Inventory and Spares

| Component | Model | Quantity | Location | Spare Available |
|-----------|-------|----------|----------|----------------|
| <!-- --> | <!-- --> | <!-- --> | <!-- --> | <!-- --> |

---

## 14. References

- See [MEASUREMENT_PROTOCOL.md](MEASUREMENT_PROTOCOL.md) for how this hardware is used in measurement procedures.
- See [PROJECT_ORGANIZATION.md](PROJECT_ORGANIZATION.md) for overall project structure.

---

**Document Status:** Template - To be expanded
**Last Updated:** February 17, 2026
