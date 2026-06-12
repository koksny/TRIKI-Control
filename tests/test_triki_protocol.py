import unittest

from triki_protocol import MotionStreamParser


class MotionStreamParserTests(unittest.TestCase):
    def test_decodes_fragmented_motion_samples_from_uart_notifications(self):
        parser = MotionStreamParser()

        self.assertEqual(parser.feed(bytes.fromhex("21 00 00 00 00")), [])
        samples = parser.feed(
            bytes.fromhex(
                "22 00 02 00 14 ff 20 00 ce 01 bd fa 43 fb"
                "22 00 ee 00 04 fe"
            )
        )
        samples += parser.feed(bytes.fromhex("14 00 28 02 a9 fa 71 fb"))

        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0].values, (2, -236, 32, 462, -1347, -1213))
        self.assertEqual(samples[1].values, (238, -508, 20, 552, -1367, -1167))

    def test_resynchronizes_to_next_motion_sample_marker(self):
        parser = MotionStreamParser()

        samples = parser.feed(bytes.fromhex("99 88 22 00 01 00 02 00 03 00 04 00 05 00 06 00"))

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].packet_id, 0)
        self.assertEqual(samples[0].values, (1, 2, 3, 4, 5, 6))

    def test_decodes_motion_sample_with_nonzero_packet_id(self):
        parser = MotionStreamParser()

        samples = parser.feed(bytes.fromhex("22 01 09 00 53 00 43 01 84 05 73 05 71 01"))

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].packet_id, 1)
        self.assertEqual(samples[0].values, (9, 83, 323, 1412, 1395, 369))


if __name__ == "__main__":
    unittest.main()
