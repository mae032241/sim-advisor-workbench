/* Benchmark input preparation: geometry builds only, never meshes or solves. */
import com.comsol.model.*;
import com.comsol.model.util.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.lang.reflect.Array;
import java.util.*;

public final class BenchmarkGeometry {
  static Map<String,Object> map(Object... a) {Map<String,Object> m=new LinkedHashMap<>();for(int i=0;i<a.length;i+=2)m.put((String)a[i],a[i+1]);return m;}
  static String quote(String s){StringBuilder b=new StringBuilder("\"");for(char c:s.toCharArray()){if(c=='"'||c=='\\')b.append('\\').append(c);else if(c<32)b.append(String.format("\\u%04x",(int)c));else b.append(c);}return b.append('"').toString();}
  static String json(Object v){if(v==null)return "null";if(v instanceof String)return quote((String)v);if(v instanceof Number||v instanceof Boolean)return v.toString();if(v instanceof Map){StringJoiner j=new StringJoiner(",","{","}");for(Object e:((Map<?,?>)v).entrySet()){Map.Entry<?,?> x=(Map.Entry<?,?>)e;j.add(quote(x.getKey().toString())+":"+json(x.getValue()));}return j.toString();}if(v instanceof Iterable){StringJoiner j=new StringJoiner(",","[","]");for(Object x:(Iterable<?>)v)j.add(json(x));return j.toString();}if(v.getClass().isArray()){StringJoiner j=new StringJoiner(",","[","]");for(int i=0;i<Array.getLength(v);i++)j.add(json(Array.get(v,i)));return j.toString();}return quote(v.toString());}
  static void write(Path p,Object value)throws Exception{Files.write(p,json(value).getBytes(StandardCharsets.UTF_8));}
  static Map<String,Object> stats(GeomSequence g){return map("dimension",g.getSDim(),"length_unit",g.lengthUnit(),"domains",g.getNDomains(),"boundaries",g.getNBoundaries(),"edges",g.getNEdges(),"vertices",g.getNVertices(),"bounding_box_native_axis_pairs",g.getBoundingBox(),"assembly",g.isAssembly());}
  public static void main(String[] args){
    try{
      Path source=Paths.get(args[0]),out=Paths.get(args[1]),privateReport=Paths.get(args[2]);
      Files.createDirectories(out);ModelUtil.initStandalone(false);ModelUtil.showProgress(false);
      Model original=ModelUtil.loadCopy("PrivateReference",source.toString());original.modelPath(source.getParent().toString());
      Model question=ModelUtil.create("Question");question.label("geometry_only.mph");question.author("");question.comments("");question.modelPath(out.toString());
      List<Object> publicGeometries=new ArrayList<>(),provenance=new ArrayList<>(),skipped=new ArrayList<>();int index=0;
      for(String component:original.component().tags()){
        if(args.length>3&&!args[3].equals("all")&&!args[3].equals(component)){skipped.add(map("component",component,"reason","not_initial_design_component"));continue;}
        for(String geom:original.component(component).geom().tags()){
          GeomSequence g=original.component(component).geom(geom);
          if(geom.contains("_xdim")||geom.startsWith("pgeom_")){skipped.add(map("component",component,"geometry",geom,"reason","physics_generated_extra_dimension"));continue;}
          long clock=System.nanoTime();Map<String,Object> saved=stats(g);String buildStatus="rebuilt_from_saved_geometry_sequence",buildError=null;
          try{g.run();}catch(Exception ex){buildStatus="stored_geometry_after_build_error";buildError=ex.getMessage();}
          if(g.getNDomains()==0&&g.getNBoundaries()==0&&g.getNEdges()==0)throw new IllegalStateException("Geometry has no usable entities: "+component+"/"+geom);
          index++;String name=String.format("geometry_%02d",index),ct="comp"+index,gt="geom"+index;
          Path nativeFile=out.resolve(name+".mphbin");g.export().setType("nativebin");g.exportFinal(nativeFile.toString());
          question.component().create(ct,true);question.component(ct).label("Part "+index);
          question.component(ct).geom().create(gt,g.getSDim());GeomSequence fresh=question.component(ct).geom(gt);fresh.label("Geometry");fresh.lengthUnit(g.lengthUnit());
          fresh.feature("fin").set("action",g.isAssembly()?"assembly":"union");
          if(g.isAssembly())fresh.feature("fin").set("createpairs",false);
          fresh.create("imp1","Import");fresh.feature("imp1").label("Initial geometry");fresh.feature("imp1").set("filename",nativeFile.getFileName().toString());fresh.run();
          Map<String,Object> item=stats(fresh);item.put("component",ct);item.put("geometry",gt);item.put("native_file",name+".mphbin");
          if(g.getSDim()==3){
            try{fresh.export().setType("step");fresh.export().setLengthUnitSTEP("m");fresh.export().selection().set(fresh.objectNames());fresh.export(out.resolve(name+".step").toString());item.put("step_file",name+".step");item.put("step_unit","m");}
            catch(Exception ex){item.put("step_status","unavailable");}
          }
          publicGeometries.add(item);provenance.add(map("source_component",component,"source_geometry",geom,"saved",saved,"built",stats(g),"neutral",item,"build_status",buildStatus,"build_error",buildError,"elapsed_seconds",(System.nanoTime()-clock)/1e9));
        }
      }
      if(index==0)throw new IllegalStateException("No initial geometry was exported");
      question.save(out.resolve("geometry_only.mph").toString());
      write(out.resolve("geometry.json"),map("schema","benchmark-initial-geometry/v1","geometries",publicGeometries,"mesh_run",false,"solver_run",false,"materials_defined",false,"physics_defined",false,"status","geometry_only"));
      write(privateReport,map("schema","benchmark-geometry-provenance/v1","comsol_version",ModelUtil.getComsolVersion(),"source",source.toString(),"geometries",provenance,"skipped",skipped,"initial_geometry_only",true,"mesh_run",false,"solver_run",false));
      ModelUtil.remove("PrivateReference");ModelUtil.remove("Question");ModelUtil.clear();System.out.println("BENCHMARK_GEOMETRY_OK");System.exit(0);
    }catch(Throwable ex){ex.printStackTrace();System.exit(2);}
  }
}
