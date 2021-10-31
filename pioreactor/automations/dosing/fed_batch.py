# -*- coding: utf-8 -*-

# pump X ml every period (minute, 30min, hour, etc.)

from pioreactor.automations.dosing.base import DosingAutomation
from pioreactor.automations import events
from pioreactor.actions.add_media import add_media


class FedBatch(DosingAutomation):
    """
    Useful for fed-batch automations
    """

    key = "fed_batch"

    published_settings = {
        "volume": {"datatype": "float", "unit": "mL", "settable": True},
    }

    def __init__(self, volume, **kwargs):
        super(FedBatch, self).__init__(**kwargs)
        self.volume = float(volume)

    def execute(self):
        add_media(
            ml=self.volume,
            source_of_event=f"{self.job_name}:{self.__class__.__name__}",
            unit=self.unit,
            experiment=self.experiment,
        )
        return events.AddMediaEvent(f"Added {self.volume}mL")
