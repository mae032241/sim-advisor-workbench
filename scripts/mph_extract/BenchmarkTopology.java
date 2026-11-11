/* Neutral geometric observations for text-only planning agents. */
import com.comsol.model.*;
import com.comsol.model.util.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.lang.reflect.*;
import java.util.*;

public final class BenchmarkTopology {
  static List<Object> entities(GeomSequence g,int dim,int count)throws Exception {
    List<Object> result=new ArrayList<>();Object measure=g.getClass().getMethod("measureFinal").invoke(g);
    Object selection=measure.getClass().getMethod("selection").invoke(measure);
    selection.getClass().getMethod("geom",int.class).invoke(selection,dim);
    for(int id=1;id<=count;id++) {
      selection.getClass().getMethod("set",int[].class).invoke(selection,(Object)new int[]{id});
      Object box=measure.getClass().getMethod("getBoundingBox").invoke(measure);Object size=null;
      String method=dim==3?"getVolume":dim==2?"getArea":dim==1?"getLength":null;
      if(method!=null)try{Object v=measure.getClass().getMethod(method).invoke(measure);if(v instanceof Number&&Double.isFinite(((Number)v).doubleValue()))size=v;}catch(Exception ignored){}
      result.add(BenchmarkGeometry.map("entity_id",id,"dimension",dim,"bounding_box_native_axis_pairs",box,"measure_native_units",size));
    }
    return result;
  }
  public static void main(String[] args){
    try {
      ModelUtil.initStandalone(false);ModelUtil.showProgress(false);
      for(int i=1;i<=18;i++) {
        Path directory=Paths.get(args[0],String.format("case-%02d",i),"inputs");
        Model q=ModelUtil.loadCopy("NeutralFacts",directory.resolve("geometry_only.mph").toString());
        List<Object> geometries=new ArrayList<>();
        for(String ct:q.component().tags())for(String gt:q.component(ct).geom().tags()){
          GeomSequence g=q.component(ct).geom(gt);geometries.add(BenchmarkGeometry.map("component",ct,"geometry",gt,"dimension",g.getSDim(),"length_unit",g.lengthUnit(),"domains",entities(g,g.getSDim(),g.getNDomains()),"boundaries",entities(g,g.getSDim()-1,g.getNBoundaries())));
        }
        Files.write(directory.resolve("topology.json"),BenchmarkGeometry.json(BenchmarkGeometry.map("schema","neutral-geometry-entities/v1","geometries",geometries,"entity_identity","fresh_geometry_only_model","geometry_run",false,"mesh_run",false,"solver_run",false)).getBytes(StandardCharsets.UTF_8));
        ModelUtil.remove("NeutralFacts");ModelUtil.clear();System.out.println("TOPOLOGY_READY case-"+String.format("%02d",i));
      }
      System.exit(0);
    }catch(Throwable e){e.printStackTrace();System.exit(2);}
  }
}
