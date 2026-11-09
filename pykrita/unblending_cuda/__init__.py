from krita import DockWidgetFactory, DockWidgetFactoryBase, Krita

from .docker import UnblendingDocker

Krita.instance().addDockWidgetFactory(
    DockWidgetFactory("unblending_cuda", DockWidgetFactoryBase.DockRight, UnblendingDocker)
)
