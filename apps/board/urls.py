from django.urls import path

from apps.board.views import (
    AirportMoveView,
    BoardMeView,
    CellCurrentView,
    CellOpenView,
    ChanceCardCatalogView,
    ChanceConfirmView,
    ChanceNowView,
    ChanceUseView,
    DiceConfirmView,
    DiceRollView,
    DiceStatusView,
    OpenedChallengesView,
)

app_name = "board"

urlpatterns = [
    path("me", BoardMeView.as_view(), name="board-me"),
    path("cell/current", CellCurrentView.as_view(), name="cell-current"),
    path("cell/open", CellOpenView.as_view(), name="cell-open"),
    path("opened_challenges", OpenedChallengesView.as_view(), name="opened-challenges"),
    path("chance/catalog", ChanceCardCatalogView.as_view(), name="chance-catalog"),
    path("chance/now", ChanceNowView.as_view(), name="chance-now"),
    path("chance/use", ChanceUseView.as_view(), name="chance-use"),
    path("chance/confirm", ChanceConfirmView.as_view(), name="chance-confirm"),
    path("dice/status", DiceStatusView.as_view(), name="dice-status"),
    path("dice/roll", DiceRollView.as_view(), name="dice-roll"),
    path("dice/confirm", DiceConfirmView.as_view(), name="dice-confirm"),
    path("airport/move", AirportMoveView.as_view(), name="airport-move"),
]
