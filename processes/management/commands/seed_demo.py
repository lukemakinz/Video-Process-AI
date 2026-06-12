from django.core.management.base import BaseCommand

from processes.models import Activity, Operation, Process


class Command(BaseCommand):
    help = "Tworzy przykładowy proces, operacje i czynności dla demo analizy wideo."

    def handle(self, *args, **options):
        process, _ = Process.objects.get_or_create(
            name="Produkcja elementu A",
            defaults={
                "description": "Proces obejmuje przygotowanie detalu, frezowanie i kontrolę.",
            },
        )

        operations = [
            (
                "Przygotowanie detalu",
                "Pobranie i przygotowanie detalu przed obróbką.",
                1,
            ),
            (
                "Frezowanie",
                "Obróbka detalu wykonywana na zamkniętej frezarce CNC.",
                2,
            ),
            (
                "Kontrola jakości",
                "Sprawdzenie detalu po zakończeniu obróbki.",
                3,
            ),
            (
                "Odkładanie gotowego elementu",
                "Odłożenie gotowego detalu w wyznaczone miejsce.",
                4,
            ),
        ]

        operation_objects = {}
        for name, description, order in operations:
            operation, _ = Operation.objects.get_or_create(
                process=process,
                name=name,
                defaults={"description": description, "order": order},
            )
            operation_objects[name] = operation

        milling = operation_objects["Frezowanie"]
        activities = [
            (
                "pobranie detalu",
                "Operator pobiera detal przeznaczony do obróbki.",
                "- operator sięga po detal\n- detal jest trzymany w dłoniach operatora",
                "- operator odkłada detal\n- operator wykonuje kontrolę detalu",
                Activity.Performer.OPERATOR,
            ),
            (
                "podejście do maszyny",
                "Operator przemieszcza się w kierunku frezarki.",
                "- operator idzie w stronę maszyny\n- detal lub narzędzie może być niesione w dłoniach",
                "- operator stoi przy maszynie i wykonuje załadunek\n- operator odchodzi od maszyny",
                Activity.Performer.OPERATOR,
            ),
            (
                "otwarcie maszyny",
                "Operator otwiera osłonę lub drzwi frezarki.",
                "- widoczny jest ruch otwierania osłony\n- przestrzeń robocza staje się dostępna",
                "- operator wkłada detal do już otwartej maszyny\n- operator zamyka osłonę",
                Activity.Performer.OPERATOR,
            ),
            (
                "załadunek detalu",
                "Operator umieszcza detal wewnątrz przestrzeni roboczej frezarki.",
                "- operator trzyma detal\n- wkłada detal do otwartej maszyny\n- dłonie operatora znajdują się w przestrzeni roboczej",
                "- operator tylko otwiera maszynę\n- operator wyjmuje detal\n- operator wykonuje kontrolę poza maszyną",
                Activity.Performer.OPERATOR,
            ),
            (
                "zamknięcie maszyny",
                "Operator zamyka osłonę frezarki po załadunku detalu.",
                "- osłona lub drzwi maszyny przemieszczają się do pozycji zamkniętej\n- operator kończy dostęp do przestrzeni roboczej",
                "- osłona jest otwierana\n- operator używa panelu sterowania",
                Activity.Performer.OPERATOR,
            ),
            (
                "uruchomienie maszyny",
                "Operator wykonuje interakcję z panelem w celu rozpoczęcia cyklu.",
                "- operator naciska przyciski lub obsługuje panel\n- interakcja następuje po zamknięciu maszyny",
                "- operator tylko czeka przy maszynie\n- maszyna już pracuje bez interakcji operatora",
                Activity.Performer.OPERATOR,
            ),
            (
                "praca maszyny",
                "Maszyna wykonuje obróbkę przy zamkniętej osłonie.",
                "- osłona jest zamknięta\n- widoczne lub słyszalne są oznaki pracy maszyny",
                "- nie można potwierdzić pracy maszyny\n- operator wykonuje ręczną czynność przy otwartej maszynie",
                Activity.Performer.MACHINE,
            ),
            (
                "oczekiwanie operatora",
                "Operator czeka bez aktywnego wykonywania czynności produkcyjnej.",
                "- operator stoi lub siedzi bez obsługi detalu\n- nie widać aktywnej interakcji z maszyną",
                "- operator idzie\n- operator obsługuje maszynę lub detal",
                Activity.Performer.OPERATOR,
            ),
            (
                "chodzenie",
                "Operator przemieszcza się między stanowiskami lub obszarami.",
                "- widoczny jest ruch chodzenia\n- operator zmienia położenie w gnieździe",
                "- operator stoi i wykonuje czynność przy maszynie\n- operator wykonuje kontrolę detalu",
                Activity.Performer.OPERATOR,
            ),
            (
                "otwarcie maszyny po zakończeniu",
                "Operator otwiera frezarkę po zakończeniu cyklu pracy.",
                "- osłona jest otwierana po pracy maszyny\n- operator uzyskuje dostęp do gotowego detalu",
                "- operator otwiera maszynę przed załadunkiem\n- operator wyjmuje detal",
                Activity.Performer.OPERATOR,
            ),
            (
                "rozładunek detalu",
                "Operator wyjmuje detal z przestrzeni roboczej frezarki.",
                "- operator sięga do wnętrza maszyny\n- detal opuszcza przestrzeń roboczą",
                "- operator wkłada detal\n- operator tylko otwiera maszynę",
                Activity.Performer.OPERATOR,
            ),
            (
                "kontrola detalu",
                "Operator ogląda lub sprawdza detal po obróbce.",
                "- operator trzyma detal poza maszyną\n- detal jest obracany lub oglądany\n- widoczna jest kontrola wizualna",
                "- operator transportuje detal\n- operator wkłada detal do maszyny",
                Activity.Performer.OPERATOR,
            ),
            (
                "inne",
                "Widoczna czynność nie mieści się w pozostałych klasach.",
                "- działanie jest widoczne, ale nie pasuje do zdefiniowanych czynności",
                "- brak wystarczających informacji; wtedy wybierz niepewne",
                Activity.Performer.UNKNOWN,
            ),
            (
                "niepewne",
                "Nie można wiarygodnie przypisać fragmentu do konkretnej czynności.",
                "- obraz lub dźwięk nie pozwala potwierdzić czynności\n- zasłonięty jest kluczowy obszar",
                "- czynność jest jednoznacznie widoczna i pasuje do innej klasy",
                Activity.Performer.UNKNOWN,
            ),
        ]

        for order, (name, description, recognition, exclusion, performer) in enumerate(activities, start=1):
            activity, _ = Activity.objects.get_or_create(
                operation=milling,
                name=name,
                defaults={
                    "description": description,
                    "recognition_rules": recognition,
                    "exclusion_rules": exclusion,
                    "performed_by": performer,
                    "order": order,
                },
            )
            if activity.order != order:
                activity.order = order
                activity.save(update_fields=["order", "updated_at"])

        self.stdout.write(self.style.SUCCESS("Dane demo zostały utworzone."))
