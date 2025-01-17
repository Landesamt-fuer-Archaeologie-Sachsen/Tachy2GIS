# -*- coding: utf-8 -*-
#******************************************************************************
#
#
#******************************************************************************
import os.path
import re
import datetime
import uuid
from PyQt5.QtCore import *
from PyQt5.QtWidgets import QApplication as qApp
from qgis.core import *
from qgis.gui import *

from . import GSI_Parser
from .. import AnchorUpdateDialog
from ..Tachy2GIS_dialog import Tachy2GisDialog

#  some helpful regular expressions for handling wkt:
## This regular expressions pulls all numbers from a single vertex
WKT_VALUES = re.compile(r"[\d\-\.]+")
## This regular expressions can be used to discard leading and trailing text from coordinates
WKT_STRIP = re.compile(r"^\D+|\D+$")
## This regular expressions can be used to drop the last coordinate from a vertex.  
#  That this is the measure is purely an assumption, this has to be used carefully.
WKT_REMOVE_MEASURE = re.compile(r" \d+(?=[,)])")

## List of wkt extensions used to indicate the available dimensions
WKT_EXTENSIONS = [' ', 'Z ', 'ZM ']

try:
    #import shapefile
    pass
except ImportError:
    print('Please install pyshp from https://pypi.python.org/pypi/pyshp/ to handle shapefiles')
    raise
## Updating anchor points that are used to enable snapping when manually adding 
#  geometries is delegated to its own thread. This class is required to do that.
#  The implementation is based on this example:
#  https://stackoverflow.com/questions/6783194/background-thread-with-qthread-in-pyqt#6789205
class AnchorUpdater(QObject):
    # The thread uses signals to communicate its progress to the AnchorUpdateDialog
    signalGeometriesProgress = pyqtSignal(int)
    signalAnchorCount = pyqtSignal(int)
    signalAnchorProgress = pyqtSignal(int)
    signalFinished = pyqtSignal()
    
    ## Constructor
    #  @param parent Not used
    #  @param layer The vector layer which is to be scanned for vertices    
    def __init__(self, parent = None, layer = None):
        super(self.__class__, self).__init__(parent)
        self.layer = layer
        self.anchorPoints = []
        self.anchorIndex = QgsSpatialIndex()
        self.abort = False
        self._mutex =QMutex()

    ## A slot to receive the 'Abort' signal from the AnchorUpdateDialog.
    @pyqtSlot()
    def abortExtraction(self):
        self._mutex.lock()
        self.abort = True
        self._mutex.unlock()

    ## The main worker method
    #  getting all features and making sure they have geometries that can be
    #  exported to wkt. This solution with expection handling has been chosen
    #  because checking 'geometry is None' leads to instant crashing (No
    #  stack trace,no exception, no nothing).
    #  The enumerator is required to update the progress bar
    def startExtraction(self):
        features = self.layer.getFeatures()
        wkts = []
        for i, feature in enumerate(features):
            geometry = feature.geometry()
            # This is ugly, I know, but as mentioned above, it works
            try:
                wkts.append(geometry.asWkt())
            except:
                pass
            self.signalGeometriesProgress.emit(i)
            # frequently forcing event processing is required to actually update
            # the progress bars and to be able to receive the abort signal 
            qApp.processEvents()
            if self.abort: 
                self.anchorPoints = []
                self.anchorIndex = QgsSpatialIndex()
                return
        allVertices = []
        
        pointIndex = 0
        self.signalAnchorCount.emit(len(wkts))
        for i, wkt in enumerate(wkts):
            # The feature wtk strings are broken into their vertices 
            for vertext in wkt.split(','):
                # a regex pulls out the numbers, of which the first three are mapped to float
                dimensions = WKT_VALUES.findall(vertext)
                coordinates = tuple(map(float, dimensions[:3]))
                # this ensures that only distinct vertices are indexed
                if coordinates not in allVertices:
                    allVertices.append(coordinates)
                    # preparing a new wkt string representing the vertex as point
                    coordText = WKT_STRIP.sub('', vertext)
                    extension = WKT_EXTENSIONS[len(dimensions) - 2]
                    anchorWkt = 'Point' + extension + '(' + coordText + ')'
                    self.anchorPoints.append(anchorWkt)
                    # creating and adding a new entry to the index. The id is 
                    # synchronized with the point list 
                    newAnchor = QgsFeature(pointIndex)
                    pointIndex += 1
                    #anchorPoint.fromWkt(anchorWkt)
                    try:  # Timmel
                        newAnchor.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(coordinates[0], coordinates[1])))
                        self.anchorIndex.insertFeature(newAnchor)
                        # Just checking if this worked:
                        testWkt = newAnchor.geometry().asWkt()
                        nn = self.anchorIndex.nearestNeighbor(QgsPointXY(coordinates[0], coordinates[1]), 1)
                        self.signalAnchorCount.emit(i + 1)
                    except:  # Timmel
                        pass  # Timmel
                qApp.processEvents()
                if self.abort: 
                    self.anchorPoints = []
                    self.anchorIndex = QgsSpatialIndex()
                    return
        self.signalFinished.emit()
        

## This class handles individual vertices for data acquisition and visualization
#
class T2G_Vertex():
    ## String constant to identify manually generated vertices
    SOURCE_INTERNAL = 'Man.'
    ## String constant to identify automatically generated vertices
    SOURCE_EXTERNAL = 'Ext.'
    ## Manually generated vertices get a 'box' icon
    SHAPE_INTERNAL = QgsVertexMarker.ICON_BOX
    ## Vertices from an external source get an 'X'
    SHAPE_EXTERNAL =QgsVertexMarker.ICON_X
    ## Mapping source keywords to the corresponding shapes.
    SHAPE_MAP = {SOURCE_INTERNAL: SHAPE_INTERNAL,
                 SOURCE_EXTERNAL: SHAPE_EXTERNAL}
    ## Headers for displaying vertices in a tableView
    HEADERS = ['#', 'Source', 'x', 'y', 'z']

    messliste = [] #Timmel

    ## Constructor
    #  @param label Point number or identifier
    #  @param source Should be one of the defined source keywords
    #  @param x,y,z Vertex coordinates
    #  @param wkt allows to pass a wkt string containing coordinates to the ctor.
    #             If at the same time x, y and z are passed, their values will be overwritten
    def __init__(self, label=None, source=None, x=None, y=None, z=0, wkt=""):
        self.label = str(label)
        self.source = source
        self.x = x
        self.y = y
        self.z = z

        self.wkt = wkt
        self.wktDimensions = 0
        if self.x is not None:
            self.wktDimensions += 1
        if self.y is not None:
            self.wktDimensions += 1
        if self.z is not None:
            self.wktDimensions += 1
        if not wkt == "":
            self.setWkt(wkt)

    ## list of fields used to feed a table model
    #  @return a list containing string for label an source and floats for x, y, z
    def fields(self):
        return [self.label, self.source, self.x, self.y, self.z]
    
    ## Returns a 2D QgsPoint.
    def getQgsPointXY(self):
        return QgsPointXY(self.x, self.y)
    
    ## Allows to set coordinates. Will die horribly if it is passed less
    #  than three coordinates
    #  @param xyz a list or tuple of coordinates
    def setXyz(self, xyz):
        self.x, self.y, self.z = xyz

    ## Sets the vertex' coordinates from Well Known Text
    #  @param wkt A string containing at least two numbers
    def setWkt(self, wkt):
        self.wkt = wkt
        # a regex extracts all numbers from the wkt
        dimensions = WKT_VALUES.findall(wkt)
        # x and y are assumed to be present
        self.x, self.y = list(map(float, dimensions[:2]))
        self.wktDimensions = len(dimensions)
        # if there are at least three dimensions, z is extracted as well
        if self.wktDimensions >= 3:
            self.z = float(dimensions[2])
        else:
            self.z = 0
    
    ## Gives you a marker to draw on the map canvas. The shape depends on the
    #  source of the vertex, to help distinguishing between manually created
    #  vertices and external ones
    #  @return a QgsVertexMarker
    def getMarker(self, canvas):
        marker = QgsVertexMarker(canvas)
        marker.setCenter(self.getQgsPointXY())
        marker.setIconType(self.SHAPE_MAP[self.source])
        return marker
    
    ## Returns the points' coordinates as a tuple. The tuple's length corresponds
    #  to the actually populated dimensions to avoid upsetting the shapefile export.
    #  @return a tuple of float that may contain two or three items
    def getCoords(self):
        if self.wktDimensions == 2:
            return (self.x, self.y)
        else:
            return (self.x, self.y, self.z)

    @staticmethod
    def fromGSI(line):
        # parser returns two dicts, values and units (actually strings to label the units)
        # this is a clear case of 'Code auf Vorrat', I apologise for this.
        vtxData = GSI_Parser.parse(line)[0]
        if 'targetZ' not in list(vtxData.keys()):
            raise ValueError('No z coordinate in: ' + line)
        label = vtxData['pointId']
        x = vtxData['targetX']
        y = vtxData['targetY']
        z = vtxData['targetZ']

        #Timmel Spiegelhöhe
        try:
            project = QgsProject.instance()
            reflH = QgsExpressionContextUtils.projectScope(project).variable('reflH')
            reflH = float(reflH)
            z = round(float(z) - reflH, 3)
        except:
            pass

        source = T2G_Vertex.SOURCE_EXTERNAL
        T2G_Vertex.messliste.append(vtxData)  # Timmel
        return T2G_Vertex(label, source, x, y, z)


## The T2G_VertexList handles the painting and selection of vertices
#  it also pulls existing vertices from a vector layer to use them as anchors when
#  manually creating vertices.
#  It also provides a model for the table view in the main dialog. This class does
#  a lot of things and can be considered the center of this plugin.
#  It won't make you breakfast though.
class T2G_VertexList(QAbstractTableModel):
    ## The color of unselected vertice
    VERTEX_COLOR = Qt.red
    ### Selected vertices get a different color
    SELECTED_COLOR = Qt.green

    snap = 0

    ## Ctor
    #  @param vertices the vertex list can be initialized with a list of vertices
    #  @param parent included to match the ctor of QAbstractTableModel
    #  @param args see above
    def __init__(self, vertices = [], parent = None, *args):
        QAbstractTableModel.__init__(self, parent, *args)
        self.columnCount = len(T2G_Vertex().fields())
        self.vertices = vertices
        self.colors = []
        self.shapes = []
        self.anchorPoints = []
        self.anchorIndex = QgsSpatialIndex()
        self.selected = None
        self.maxIndex = None
        self.updateThread = QThread()
        self.anchorUpdater = None

    ## Reimplemented from QAbstractTableModel
    def rowCount(self, *args, **kwargs):
        return len(self)

    ## Reimplemented from QAbstractTableModel
    def columnCount(self, *args, **kwargs):
        return self.columnCount
    
    ## Reimplemented from QAbstractTableModel
    def data(self, index, role):
        if Qt is None:
            return
        if not index.isValid():
            return
        elif role != Qt.DisplayRole:
            return
        row = index.row()
        col = index.column()
        vertex = self.vertices[row]
        field = vertex.fields()[col]
        return field
    
    ## Reimplemented from QAbstractTableModel
    def headerData(self, section, orientation, role):
        if Qt is None:
            return
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            headers = T2G_Vertex.HEADERS
            return headers[section]
        return QAbstractTableModel.headerData(self, section, orientation, role)

    ## this method updates anchor points for snapping
    #  extracting all vertices from the geometries in a layer may take some time,
    #  so the method reports its progress via a dialog that allows aborting the 
    #  operation when it takes too long.
    #  @param layer: The currently active layer
    def updateAnchors(self, layer):
        #  Snapping is driven by a spatial index of all distinct existing vertices
        #  the index is 2D only, so 3-4D information has to be stored elsewhere
        #  (self.anchorPoints in this case).
        #  self.anchorPoints holds wkt representation of all vertices that can be 
        #  passed to the ctor of a new T2G_Vertex. 
        self.anchorIndex = QgsSpatialIndex()
        self.anchorPoints = []
        if layer is None:
            # empty or nonexisting layers leave us with an empty point list and index.
            return
        # Initializing the progress dialog
        aud = AnchorUpdateDialog.AnchorUpdateDialog()
        aud.abortButton.clicked.connect(self.abortUpdate)
        aud.geometriesBar.setMaximum(layer.featureCount())
        aud.geometriesBar.setValue(0)
        aud.anchorBar.setValue(0)
        # the layer is passed to the anchorUpdater and the updater is moved to
        # a new thread
        self.anchorUpdater = AnchorUpdater(layer=layer)
        self.anchorUpdater.moveToThread(self.updateThread)
        self.updateThread.start()
        self.anchorUpdater.signalAnchorCount.connect(aud.setAnchorCount)
        self.anchorUpdater.signalAnchorProgress.connect(aud.anchorProgress)
        self.anchorUpdater.signalGeometriesProgress.connect(aud.geometriesProgress)
        # the dialog is displayed and extraction is started
        aud.show()
        self.anchorUpdater.startExtraction()
        # the abort method of the updater clears its index and points list, so
        # we can just use them, even if we aborted. They will be empty in that 
        # case. 
        self.anchorIndex = self.anchorUpdater.anchorIndex
        self.anchorPoints = self.anchorUpdater.anchorPoints
        
    ## checks if there are anchors
    #  @return bool 
    def hasAnchors(self):
        return len(self.anchorPoints) > 0
    
    ## Tells the update thread to stop and cleans up after it
    @pyqtSlot()
    def abortUpdate(self):
        if self.updateThread.isRunning():
            # abortExtraction lets the process exit the extraction loop and 
            # resets its anchorIndex and anchorPoints
            self.anchorUpdater.abortExtraction()
            self.updateThread.terminate()
            self.updateThread.wait()
    
    ## makes the vertexList look more like a regular list
    #  @return an int representing the number of vertices on the list
    def __len__(self):
        return self.vertices.__len__()
    
    ## allows to access vertices by index: 'vertexList[2]' works as expected. 
    #  @param index the index of the requested vertex
    #  @return the T2G_Vertex at 'index'
    def __getitem__(self, index):
        return self.vertices.__getitem__(index)
    
    ## Allows appending new vertices. Manually created vertices will be snapped
    #  to the nearest anchor. 
    #  @param vertex the vertex to append, is assumed to be of type T2G_Vertex
    #  @return the probably modified vertex
    def append(self, vertex):

        if vertex.source == T2G_Vertex.SOURCE_INTERNAL:
            anchorId = self.anchorIndex.nearestNeighbor(vertex.getQgsPointXY(), T2G_VertexList.snap)
            # nearestNeighbour returns a list. It is not unpacked yet, because 
            # doing so causes errors if it is empty, also index '0' is interpreted
            # as 'False' and gets ignored
            if anchorId:
                wkt = self.anchorPoints[anchorId[0]]
                vertex.setWkt(wkt)
        self.vertices.append(vertex)
        self.layoutChanged.emit()
        return vertex
    
    ## removes a vertex and emits a signal to update the tableView
    def deleteVertex(self, index):
        del self.vertices[index]
        self.layoutChanged.emit()
        for i, vertex in enumerate(self.vertices): #Timmel
            if i == self.selected:
                T2G_Vertex.messliste.pop(i)


    def select(self, index):
        if index >= len(self):
            return
        self.selected = index
        
    def clearSelection(self):
        self.selected = None
    
    ## This method is used to get colors for vertex markers that let you 
    #  distinguish between selected and unselected vertices
    #  @return a list of QColors
    def getColors(self):
        colors = []
        for i, vertex in enumerate(self.vertices):
            if i == self.selected:
                colors.append(self.SELECTED_COLOR)
            else:
                colors.append(self.VERTEX_COLOR)
        return colors
    
    ## Deletes all vertices and emits a signal to update the tableView
    def clear(self):
        self.vertices = []
        self.layoutChanged.emit()
    
    ## returns the coordinates of all vertices in a format that is useful for
    #  shapefile.writer
    #  @return a double nested list of coordinates. 
    def getParts(self):
        return [[v.getCoords() for v in self.vertices]]
    
    ## creates a shapefile.writer to add a polygon geometry to a shapefile
    #  @param reader a shapefile.reader that represents the shapefile that is being worked on
    #  @return a writer that has been used to add the geometry and now waits for attributes to be added 
    def writePoly(self, targetLayer):
        vertexParts = self.getParts()
        self.addPolyLine3D(targetLayer, vertexParts[0], [])

    ## creates a shapefile.writer to add a linestring geometry to a shapefile
    #  @param reader a shapefile.reader that represents the shapefile that is being worked on
    #  @return a writer that has been used to add the geometry and now waits for attributes to be added 
    def writeLine(self, targetLayer):
        vertexParts = self.getParts()
        self.addLine3D(targetLayer, vertexParts[0], [])

    ## creates a shapefile.writer to add a point geometry to a shapefile
    #  @param reader a shapefile.reader that represents the shapefile that is being worked on
    #  @return a writer that has been used to add the geometry and now waits for attributes to be added 
    def writePoint(self, targetLayer):
        coords = self.vertices[self.selected].getCoords()
        self.addPoint3D(targetLayer, coords, [])


    def addMesspunkt(self, coords):
        layer = QgsProject.instance().mapLayersByName('Messpunkte')[0]
        project = QgsProject.instance()
        reflH = QgsExpressionContextUtils.projectScope(project).variable('reflH')
        if reflH != None:
            reflH = float(reflH)
        pt = QgsPoint(float(coords[0]), float(coords[1]), float(coords[2]))

        attList = {'ptnr': '0' , 'x': coords[0], 'y': coords[1], 'z': coords[2], 'reflH': reflH}
        self.addPoint3D(layer, coords, attList)
        #QgsMessageLog.logMessage(str(''), 'T2G-Messpunkte', Qgis.Info)

    def addPolyLine3D(self, layer, coords, attList):
        layer.startEditing()
        feature = QgsFeature()
        fields = layer.fields()
        feature.setFields(fields)

        ptList = []
        for a in coords:
            item = QgsPoint(a[0], a[1], a[2])
            ptList.append(item)
            #self.addMesspunkt(a)
        #ersten Punkt als letzten einfügen
        ptList.append(ptList[0])

        feature.setGeometry(QgsGeometry.fromPolyline(ptList))
        # Attribute
        layer.dataProvider().addFeatures([feature])
        layer.updateExtents()
        features = [feature for feature in layer.getFeatures()]
        lastfeature = features[-1]

        for item in attList:
            # QgsMessageLog.logMessage(str(item), 'T2G', Qgis.Info)
            fIndex = layer.dataProvider().fieldNameIndex(item)
            layer.changeAttributeValue(lastfeature.id(), fIndex, attList[item])

        self.addStaticAttribut(layer, lastfeature)
        pass

    def addLine3D(self, layer, coords, attList):
        layer.startEditing()
        feature = QgsFeature()
        fields = layer.fields()
        feature.setFields(fields)

        ptList = []
        for a in coords:
            item = QgsPoint(a[0] ,a[1] ,a[2])
            ptList.append(item)
            #self.addMesspunkt(a)

        feature.setGeometry(QgsGeometry.fromPolyline(ptList))
        # Attribute
        layer.dataProvider().addFeatures([feature])
        layer.updateExtents()
        features = [feature for feature in layer.getFeatures()]
        lastfeature = features[-1]

        for item in attList:
            # QgsMessageLog.logMessage(str(item), 'T2G', Qgis.Info)
            fIndex = layer.dataProvider().fieldNameIndex(item)
            layer.changeAttributeValue(lastfeature.id(), fIndex, attList[item])
        self.addStaticAttribut(layer, lastfeature)

    def addPoint3D(self, layer, coords, attList):
        layer.startEditing()
        feature = QgsFeature()
        fields = layer.fields()
        feature.setFields(fields)
        pt = QgsPoint(float(coords[0]), float(coords[1]), float(coords[2]))
        feature.setGeometry(QgsGeometry(pt))
        # Attribute
        layer.dataProvider().addFeatures([feature])
        layer.updateExtents()
        features = [feature for feature in layer.getFeatures()]
        lastfeature = features[-1]

        for item in attList:
            #QgsMessageLog.logMessage(str(item), 'T2G', Qgis.Info)
            fIndex = layer.dataProvider().fieldNameIndex(item)
            layer.changeAttributeValue(lastfeature.id(), fIndex, attList[item])

        #layer.commitChanges()
        self.addStaticAttribut(layer, lastfeature)


    def addStaticAttribut(self, targetLayer, feature):

        # Projektvariablen holen
        project = QgsProject.instance()
        idfeld = targetLayer.dataProvider().fieldNameIndex('id')
        nextid = targetLayer.maximumValue(idfeld)
        if nextid != None:
            nextid = nextid + 1
        else:
            nextid = 0

        targetLayer.startEditing()
        if QgsExpressionContextUtils.projectScope(project).variable('autoAttribute') == 'True':
            attrName = ['obj_type', 'obj_art', 'schnitt_nr', 'bef_nr', 'prof_nr', 'planum',  'fund_nr', 'prob_nr',
                        'material', 'planum']
            for elm in attrName:
                wert = QgsExpressionContextUtils.projectScope(project).variable(str(elm))
                indx = targetLayer.dataProvider().fieldNameIndex(str(elm))
                targetLayer.changeAttributeValue(feature.id(), indx, wert)

        # Layer zum schreiben öffnen
        #targetLayer.startEditing()
        UUid = targetLayer.dataProvider().fieldNameIndex('uuid')
        targetLayer.changeAttributeValue(feature.id(), UUid, '{' + str(uuid.uuid4()) + '}')
        targetLayer.changeAttributeValue(feature.id(), 0, int(nextid))
        aktcode = QgsExpressionContextUtils.projectScope(project).variable('aktcode')
        geoarch = QgsExpressionContextUtils.projectScope(project).variable('geo-arch')
        # weitere automatische Attribute
        idMesDatum = targetLayer.dataProvider().fieldNameIndex('messdatum')
        idAktCode = targetLayer.dataProvider().fieldNameIndex('aktcode')
        idgeoarch = targetLayer.dataProvider().fieldNameIndex('geo-arch')

        targetLayer.changeAttributeValue(feature.id(), idMesDatum, str(datetime.datetime.now()))
        targetLayer.changeAttributeValue(feature.id(), idAktCode, aktcode)
        targetLayer.changeAttributeValue(feature.id(), idgeoarch, geoarch)

        QgsExpressionContextUtils.setProjectVariable(QgsProject.instance(), 'SignalGeometrieNeu', 'True' + '_' + str(feature.id()))
        # Attribute aktualisieren und speichern
        #targetLayer.updateExtents()
        #targetLayer.commitChanges()


    ## Turns the vertices into geometry and writes them to a shapefile
    #  @param targetLayer a vectordatalayer that is suspected to be based on a
    #  shapefile (required to do so, actually)
    #  @param fieldData the attributes of the new feature as list
    def dumpToFile(self, targetLayer, fieldData):
        # nonexistant and non-shapefile layers will be ignored
        if targetLayer is None:
            return
        if not targetLayer.dataProvider().name() == 'ogr':
            return
        if targetLayer.geometryType() == QgsWkbTypes.PointGeometry:
            self.writePoint(targetLayer)
        if targetLayer.geometryType() == QgsWkbTypes.LineGeometry:
            self.writeLine(targetLayer)
        if targetLayer.geometryType() == QgsWkbTypes.PolygonGeometry:
            self.writePoly(targetLayer)


if __name__ == '__main__':
    testLine16 = '*11....+0000000000000306 21.022+0000000002264250 22.022+0000000009831450 31..00+0000000000002316 81..00+0000000565386572 82..00+0000005924616673 83..00+0000000000005367 87..10+0000000000000000 \r\n'
    vtx = T2G_Vertex.fromGSI(testLine16)
    print(vtx.fields())

