"""Tests for firmware/config/hardware.py — pin map and constants."""

from utils.config import (
    ADC_CH_ORP,
    # ADC channels
    ADC_CH_PH,
    ADC_CH_TDS,
    ADC_CH_TURBIDITY,
    ADS1115_ADDR,
    ADS1115_ALERT_RDY,
    # Fan
    FAN_EN,
    # UART / GPS
    GPS_BAUD,
    GPS_EXTINT,
    # I2C
    I2C_BUS,
    LED_ERROR,
    LED_GPS_FIX,
    LED_HEARTBEAT,
    LED_LORA_TX,
    LED_PINS,
    LORA_BUSY,
    LORA_DIO1,
    LORA_FREQUENCY,
    LORA_RST,
    LORA_SYNC_WORD,
    # Analog constants
    NERNST_SLOPE_25C,
    PH_VREF,
    # Relays
    RELAY_PINS,
    # SPI / LoRa
    SPI_CS,
    TDS_DIVIDER_RATIO,
    TURB_NTU_MAX,
    TURB_V_CLEAR,
)


class TestPinAssignments:
    """Verify BCM pin numbers match the schematic."""

    def test_i2c_bus(self):
        assert I2C_BUS == 1

    def test_ads1115_address(self):
        assert ADS1115_ADDR == 0x48

    def test_adc_channels_are_distinct(self):
        channels = [ADC_CH_PH, ADC_CH_TDS, ADC_CH_TURBIDITY, ADC_CH_ORP]
        assert len(set(channels)) == 4
        assert all(0 <= ch <= 3 for ch in channels)

    def test_relay_pins(self):
        assert RELAY_PINS == (17, 27, 22, 23)

    def test_led_pins(self):
        assert LED_PINS == (24, 25, 12, 13)

    def test_led_function_aliases(self):
        assert LED_HEARTBEAT == 24
        assert LED_LORA_TX == 25
        assert LED_GPS_FIX == 12
        assert LED_ERROR == 13

    def test_fan_pin(self):
        assert FAN_EN == 21

    def test_lora_pins(self):
        assert SPI_CS == 8
        assert LORA_RST == 18
        assert LORA_BUSY == 20
        assert LORA_DIO1 == 16

    def test_gps_extint(self):
        assert GPS_EXTINT == 19

    def test_no_pin_collisions(self):
        """All GPIO pins must be unique."""
        all_pins = (
            list(RELAY_PINS)
            + list(LED_PINS)
            + [
                FAN_EN,
                SPI_CS,
                LORA_RST,
                LORA_BUSY,
                LORA_DIO1,
                GPS_EXTINT,
                ADS1115_ALERT_RDY,
            ]
        )
        assert len(all_pins) == len(set(all_pins))


class TestAnalogConstants:
    """Verify analog signal chain constants."""

    def test_nernst_slope(self):
        assert abs(NERNST_SLOPE_25C - 0.05916) < 0.001

    def test_ph_reference_voltage(self):
        assert abs(PH_VREF - 2.048) < 0.001

    def test_tds_divider_ratio(self):
        assert abs(TDS_DIVIDER_RATIO - 0.3125) < 0.001

    def test_turbidity_range(self):
        assert TURB_V_CLEAR > 0
        assert TURB_NTU_MAX == 3000.0


class TestLoRaParams:
    """Verify LoRa radio parameters."""

    def test_frequency_915mhz(self):
        assert LORA_FREQUENCY == 915_000_000

    def test_sync_word_lorawan(self):
        assert LORA_SYNC_WORD == 0x3444

    def test_gps_baud(self):
        assert GPS_BAUD == 9600
